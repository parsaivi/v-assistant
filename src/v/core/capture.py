"""Capture: world → vault. The highest-value layer in the system.

Design rules, all inherited from the design doc:
  - append-only to existing notes; never rewrite a block unattended
  - low confidence goes to today's note, never to a guessed target — and never
    to a question: capture is a one-message operation, and a prompt in front of
    it is a prompt in front of every message V doesn't recognise
  - every write is a `[bot]` git commit, so everything is undoable
  - a duplicate check runs on every capture, for free, using local embeddings
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import notes, provenance, router, schema, templates
from .config import Settings, settings as default_settings
from .vaultfs import Vault, Change


@dataclass
class CaptureResult:
    intent: str
    target: str
    change: Change | None
    message: str
    duplicates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.change is not None


class Capture:
    def __init__(self, vault: Vault | None = None, settings: Settings | None = None,
                 rag=None):
        self.s = settings or default_settings
        self.vault = vault or Vault(self.s)
        self.rag = rag

    # -----------------------------------------------------------------
    def capture(self, text: str, route: router.Route, *,
                when: dt.datetime | None = None,
                confirmed: bool = False,
                extra: dict | None = None) -> CaptureResult:
        when = when or dt.datetime.now()
        extra = extra or {}
        text = text.strip()

        # A weak guess files to today's note instead of a guessed target — a
        # log line is the one bucket that is never *wrong*, only unspecific,
        # and "no, that was a lyric" moves it in one message. What this used to
        # do was stop and ask, which put a modal question in front of every
        # message V didn't recognise, including the ones that weren't captures.
        unsure = not confirmed and route.confidence < self.s.confidence_threshold
        intent = router.CAPTURE_NOTE if unsure else route.intent

        dupes = self._duplicates(text)
        handler = {
            router.CAPTURE_LYRIC: self._lyric,
            router.CAPTURE_TASK: self._task,
            router.CAPTURE_PLACE: self._place,
            router.CAPTURE_PERSON: self._person,
            router.CAPTURE_WORKOUT: self._workout,
            router.CAPTURE_EXPENSE: self._expense,
            router.CAPTURE_DECISION: self._decision,
            router.CAPTURE_IDEA: self._idea,
            router.CAPTURE_LINK: self._link,
        }.get(intent, self._daily)

        result = handler(text, route, when, extra)
        result.intent = intent
        result.duplicates = dupes
        if unsure and route.intent != router.CAPTURE_NOTE:
            # Name the guess that lost, so the correction is one word away.
            guess = _label(route.intent)
            result.message += (
                f"\n\nWasn't sure ({route.confidence:.0%} on {guess}) — "
                f"say *\"that was a {guess.lower()}\"* to move it.")
        return result

    def _duplicates(self, text: str, threshold: float = 0.55
                    ) -> list[tuple[str, float]]:
        """'Have I written about this before?' — free, runs on every capture."""
        if not self.rag or len(text.split()) < 6:
            return []
        try:
            res = self.rag.similar_to_text(text, k=3)
        except Exception:
            return []
        return [(h.rel, h.score) for h in res.hits if h.score >= threshold]

    # ---- targets -----------------------------------------------------
    def daily_path(self, day: dt.date) -> str:
        return (f"{self.s.folders.daily}/{day.year}/"
                f"{day.strftime('%m - %B')}/{day.isoformat()}.md")

    def ensure_daily(self, day: dt.date) -> str:
        """Create today's daily note, schema-valid, if it doesn't exist yet.

        Only fires when the assistant gets there before the user does. Once the
        note exists this is a no-op, so on a normal day Templater's folder
        template builds the note and this never runs.

        Which is exactly why it fills `05 - Templates/Daily Note.md` rather than
        a heading set written out here. The two used to be near-copies and the
        copy had drifted: no `## 📅 Calendar` for the calendar sync to write
        into, no Work/University/Life split under Tasks, no `### Blockers`. On
        the rare day V got there first you ended up with a visibly poorer note
        than every other day of the month. The inline body below is now only a
        fallback for a vault with no templates folder.
        """
        rel = self.daily_path(day)
        if self.vault.exists(rel):
            return rel
        from .weekplan import week_label
        # The week runs Sat → Fri and its note is named for its Saturday, so a
        # Monday belongs to the week that began two days before — not to the
        # ISO week the Monday sits in, which is next week's note.
        meta = {
            "date": day.isoformat(),
            "week": week_label(day),
            "month": day.strftime("%Y-%m"),
            "quarter": f"{day.year}-Q{(day.month - 1) // 3 + 1}",
            "mood": None, "energy": None,
            "tags": ["daily-note"],
        }
        # The template computes its values in a `<%* … %>` block, which does
        # not run here, and prints them by bare name — `<% heading %>`,
        # `<% week %>`. Supply the names, or the note reads `# ` above
        # `![[#Weekly Focus]]`. `title` is the ISO date, as `tp.file.title` is.
        context = {
            "week": meta["week"],
            "month": meta["month"],
            "quarter": meta["quarter"],
            "heading": templates.moment_format(day, "dddd, MMMM D, YYYY"),
        }
        filled = templates.fill("daily", meta, title=day.isoformat(), when=day,
                                context=context, settings=self.s)
        if filled:
            meta, body = filled
        else:
            body = (
                f"\n# {day.strftime('%A, %d %B %Y')}\n\n"
                f"> [!tip] Weekly Focus\n"
                f"> ![[{meta['week']}#Weekly Focus]]\n\n"
                f"## {self.s.daily_task_heading}\n\n"
                "## Work Log\n\n### Issues / MRs Touched\n\n"
                f"## Gym\n\n## {self.s.daily_log_heading}\n\n## End of Day\n"
            )
        # Parsa's template, filled by rules: no model wrote a word of it.
        self.vault.create(rel, meta, body, summary=f"scaffold daily note {day}",
                          source=provenance.PARSA)
        return rel

    # ---- handlers ----------------------------------------------------
    def _daily(self, text, route, when, extra) -> CaptureResult:
        rel = self.ensure_daily(when.date())
        line = f"- {when.strftime('%H:%M')} — {text}"
        # `file_rel` is set by a transport that has already downloaded and
        # saved an attachment (Telegram, so far) via `vault.save_binary`. The
        # embed rides in the same line rather than a second append, so a
        # photo with no caption still reads as one entry, not a bare
        # timestamp followed by an orphan image two lines later.
        if extra.get("file_rel"):
            line += f"\n  ![[{extra['file_rel']}]]"
        change = self.vault.append(rel, line, heading=self.s.daily_log_heading,
                                   summary=f"log entry in {when.date()}")
        return CaptureResult(route.intent, rel, change,
                             f"Logged to today's note ({when.strftime('%H:%M')}).")

    def _lyric(self, text, route, when, extra) -> CaptureResult:
        title = _title_from(text)
        rel = f"{self.s.folders.lyrics}/{when.date()} {title}.md"
        n = 2
        while self.vault.exists(rel):
            rel = f"{self.s.folders.lyrics}/{when.date()} {title} ({n}).md"
            n += 1
        meta = {
            "type": "lyric-fragment",
            "date": when.date().isoformat(),
            "mood": extra.get("mood"),
            "theme": extra.get("theme"),
            "audio": extra.get("audio"),
            "tags": ["music", "lyrics"],
        }
        body = f"\n{text}\n"
        if extra.get("audio"):
            body += f"\n![[{extra['audio']}]]\n"
        change = self.vault.create(rel, meta, body, summary=f"lyric fragment {title}")
        return CaptureResult(route.intent, rel, change,
                             f"Saved as a lyric fragment → {rel}")

    def _task(self, text, route, when, extra) -> CaptureResult:
        rel = self.ensure_daily(when.date())
        cleaned = re.sub(r"^(remind me to|i need to|i should|todo:?)\s*", "", text,
                         flags=re.I).strip()
        change = self.vault.append(rel, f"- [ ] {cleaned}",
                                   heading=self.s.daily_task_heading,
                                   summary="task captured")
        return CaptureResult(route.intent, rel, change,
                             f"Added as a task in today's note: {cleaned}")

    def _place(self, text, route, when, extra) -> CaptureResult:
        name = extra.get("name") or _title_from(text)
        rel = f"{self.s.folders.locations}/{name}.md"
        meta = schema.template_for("place")
        meta.update({
            "type": "place", "name": name,
            "category": extra.get("category", "cafe"),
            "location": extra.get("location"),
            "neighborhood": extra.get("neighborhood"),
            "city": extra.get("city", "Tehran"),
            "rating": extra.get("rating"),
            "price": extra.get("price"),
            "visited": when.date().isoformat(),
            "good-for": extra.get("good_for"),
            "wifi": extra.get("wifi"), "noise": extra.get("noise"),
            "outdoor-seating": extra.get("outdoor"),
            "open-late": extra.get("open_late"),
            "tags": ["place", extra.get("category", "cafe")],
        })
        if self.vault.exists(rel):
            change = self.vault.append(rel, f"\n- {when.date()} — {text}",
                                       heading="Visits",
                                       summary=f"visit logged at {name}")
            return CaptureResult(route.intent, rel, change,
                                 f"Logged another visit to {name}.")
        # The Place template carries the mapview block and the dataviewjs
        # visit-history query. A note written without them is a note whose map
        # pin and visit count never appear, which is most of what the type is
        # for — so what was captured goes under Experience and the template
        # supplies the rest.
        filled = templates.fill("place", meta, title=name, when=when.date(),
                                settings=self.s)
        if filled:
            meta, body = filled
            body = templates.insert_under(body, "experience", text)
        else:
            body = f"\n{text}\n\n## Visits\n"
        change = self.vault.create(rel, meta, body,
                                   summary=f"place note {name}")
        missing = [k for k in ("rating", "wifi", "noise") if meta.get(k) is None]
        msg = f"Created a Place note for {name}."
        if missing:
            msg += f" Still blank: {', '.join(missing)}."
        return CaptureResult(route.intent, rel, change, msg)

    def _person(self, text, route, when, extra) -> CaptureResult:
        name = extra.get("name") or _person_from(text)
        if not name:
            return self._daily(text, route, when, extra)
        rel = f"{self.s.folders.crm}/{name}.md"
        entry = f"- {when.date()} — {text}"
        if self.vault.exists(rel):
            heading = templates.heading_for("interaction",
                                            self.vault.read(rel).body)
            change = self.vault.append(rel, entry, heading=heading,
                                       summary=f"interaction with {name}")
            self.vault.set_meta(rel, {"last_contact": when.date().isoformat()},
                                summary=f"last_contact for {name}")
            return CaptureResult(route.intent, rel, change,
                                 f"Logged an interaction with {name}.")
        meta = {"type": "person", "aliases": [], "relation_type": None,
                "tier": 3, "frequency_days": 90,
                "last_contact": when.date().isoformat(),
                "birthday": None, "company": None, "role": None,
                "location": None, "tags": ["person", "crm"]}
        filled = templates.fill("person", meta, title=name, when=when.date(),
                                settings=self.s)
        if filled:
            meta, body = filled
            body = templates.insert_under(body, "interaction", entry)
        else:
            body = f"\n## Interactions\n\n{entry}\n"
        change = self.vault.create(rel, meta, body,
                                   summary=f"CRM note for {name}")
        return CaptureResult(route.intent, rel, change,
                             f"Created a CRM note for {name}.")

    def _workout(self, text, route, when, extra) -> CaptureResult:
        kind = extra.get("type_of_training") or _workout_type(text)
        rel = f"{self.s.folders.workouts}/{when.date()} {kind}.md"
        meta = {"type": "workout", "date": when.date().isoformat(),
                "type-of-training": kind, "duration": extra.get("duration"),
                "energy-level": extra.get("energy"), "tags": ["gym"]}
        if self.vault.exists(rel):
            change = self.vault.append(rel, f"- {text}", heading="Exercises",
                                       summary=f"workout detail {when.date()}")
            return CaptureResult(route.intent, rel, change,
                                 f"Appended to today's {kind} session.")
        change = self.vault.create(rel, meta, f"\n## Exercises\n\n- {text}\n",
                                   summary=f"workout {when.date()} {kind}")
        return CaptureResult(route.intent, rel, change,
                             f"Logged a {kind} session for {when.date()}.")

    def _expense(self, text, route, when, extra) -> CaptureResult:
        rel = f"{self.s.folders.finance}/{when.strftime('%Y-%m')}.md"
        amount = route.slots.get("amount_text", "")
        amount_toman = route.slots.get("amount_toman")
        direction = extra.get("direction") or route.slots.get("direction", "expense")
        category = (extra.get("category") or route.slots.get("category")
                    or {"income": "income", "savings": "savings"}.get(
                        direction, "uncategorized-purchase"))
        heading = {"income": "Income", "savings": "Savings & Investment",
                   "transfer": "Transfers"}.get(direction, "Expenses")
        fields = f"[category:: {category}] [direction:: {direction}]"
        if amount_toman is not None:
            fields += f" [amount:: {amount_toman}]"
        line = f"- {when.date()} {text} {fields}"
        if not self.vault.exists(rel):
            meta = {"type": "ledger", "month": when.strftime("%Y-%m"),
                    "tags": ["finance"]}
            body = f"\n## {heading}\n\n{line}\n"
            change = self.vault.create(rel, meta, body,
                                       summary=f"ledger {when.strftime('%Y-%m')}")
        else:
            change = self.vault.append(rel, line, heading=heading,
                                       summary=f"{direction} logged")
        return CaptureResult(route.intent, rel, change,
                             f"Logged the {direction}{' (' + amount + ')' if amount else ''}.")

    def _decision(self, text, route, when, extra) -> CaptureResult:
        project = extra.get("project") or _project_from(text, self.s)
        if project:
            rel = f"{project}/08 - Decision Log.md"
            if self.vault.exists(rel):
                entry = f"\n### {when.date()} — {_title_from(text)}\n\n{text}\n"
                change = self.vault.append(rel, entry,
                                           summary=f"decision logged in {project}")
                return CaptureResult(route.intent, rel, change,
                                     f"Appended to the {project} decision log.")
        return self._daily(text, route, when, extra)

    def _idea(self, text, route, when, extra) -> CaptureResult:
        rel = f"{self.s.folders.braindump}/{when.strftime('%Y-%m')} ideas.md"
        entry = f"\n### {when.strftime('%Y-%m-%d %H:%M')}\n\n{text}\n"
        if not self.vault.exists(rel):
            change = self.vault.create(
                rel, {"type": "idea-dump", "month": when.strftime("%Y-%m"),
                      "tags": ["braindump"]}, entry, summary="idea dump created")
        else:
            change = self.vault.append(rel, entry, summary="idea captured")
        return CaptureResult(route.intent, rel, change, "Filed under brain dump.")

    def _link(self, text, route, when, extra) -> CaptureResult:
        url = route.slots.get("url") or extra.get("url", "")
        folder, kind = _link_target(url, self.s)
        title = extra.get("title") or _slug_from_url(url)
        rel = f"{folder}/{title}.md"
        n = 2
        while self.vault.exists(rel):
            rel = f"{folder}/{title} ({n}).md"
            n += 1
        note_text = text.replace(url, "").strip()
        meta = {"type": kind, "url": url, "added": when.date().isoformat(),
                "status": "unopened", "rating": None,
                "tags": ["anti-library", kind]}
        body = f"\n{url}\n" + (f"\n{note_text}\n" if note_text else "")
        change = self.vault.create(rel, meta, body, summary=f"{kind} link saved")
        return CaptureResult(route.intent, rel, change,
                             f"Saved to {folder.split('/')[-1]}.")


# --- helpers ---------------------------------------------------------
def _label(intent: str) -> str:
    return {
        router.CAPTURE_NOTE: "Today's note", router.CAPTURE_LYRIC: "Lyric",
        router.CAPTURE_TASK: "Task", router.CAPTURE_PLACE: "Place",
        router.CAPTURE_PERSON: "Person", router.CAPTURE_WORKOUT: "Workout",
        router.CAPTURE_EXPENSE: "Expense", router.CAPTURE_DECISION: "Decision",
        router.CAPTURE_IDEA: "Idea", router.CAPTURE_LINK: "Link",
        router.REMINDER: "Reminder", router.QUERY: "Question",
    }.get(intent, intent)


def _title_from(text: str, max_words: int = 6) -> str:
    first = text.strip().splitlines()[0]
    words = re.sub(r"[^\w\s؀-ۿ-]", "", first).split()[:max_words]
    return " ".join(words) or "untitled"


def _person_from(text: str) -> str | None:
    m = re.search(r"(?:with|to|met)\s+([A-Z][\w]+(?:\s+[A-Z][\w]+)?)", text)
    return m.group(1) if m else None


def _workout_type(text: str) -> str:
    """Name the session from the lift, not just from the body part.

    Nobody logs "chest day" — they log "bench 3x8 at 80kg", which used to file
    as a generic *Session* and made every gym note look identical on the
    dashboard. Match the exercises people actually name.
    """
    low = text.lower()
    for keys, label in (
        (("chest", "bench", "incline", "fly", "flye", "dip"), "Chest"),
        (("back", "row", "pulldown", "pull-up", "pullup", "chin-up", "lat"), "Back"),
        (("leg", "squat", "lunge", "deadlift", "calf", "hamstring", "quad"), "Legs"),
        (("shoulder", "ohp", "overhead press", "lateral raise", "delt"), "Shoulders"),
        (("arm", "curl", "tricep", "bicep", "skullcrusher"), "Arms"),
        (("cardio", "run", "ran", "treadmill", "cycling", "bike", "row erg"), "Cardio"),
        (("full body", "fullbody"), "Full Body"),
        (("push",), "Push"),
        (("pull",), "Pull"),
    ):
        if any(k in low for k in keys):
            return label
    return "Session"


def _project_from(text: str, s: Settings) -> str | None:
    low = text.lower()
    for folder in ("15 - DUNG", "16 - Leviathan", "18 - TA Grader",
                   "19 - Assistant", "20 - Melt", "17 - AIT"):
        name = folder.split(" - ", 1)[1].lower()
        if name in low:
            return folder
    return None


def _link_target(url: str, s: Settings) -> tuple[str, str]:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return s.folders.youtube, "youtube"
    if "imdb.com" in u or "letterboxd" in u:
        return s.folders.movies, "movie"
    if "goodreads" in u or "book" in u:
        return s.folders.books, "book"
    return s.folders.antilibrary, "link"


def _slug_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    tail = re.sub(r"[^\w\s-]", " ", tail).strip()
    return (tail[:60] or "link").replace("_", " ")
