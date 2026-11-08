"""The vault's calendar — one file per month, and the only source of truth.

Before this, "what meetings do I have tomorrow?" had three possible answers and
no authority: a remote iCal feed that only `daily_calendar.py` could read, a
`## 📅 Calendar` section in each daily note that said `_No events scheduled._`
because nothing ever filled it, and the assistant's own reminders table, which
lived in SQLite where Obsidian cannot see it. So V answered "I don't have
access to an external calendar" to a question about the user's own notes.

The fix is a plain Markdown file:

    01 - Daily Notes/Calendar/2026-08.md

    ## Events
    - 2026-08-08 12:30-13:30 · 1on1 with teamlead @Room 3
    - 2026-08-09 all-day · Public holiday

Everything else *derives* from it. The remote feed imports into it; the daily
note's Calendar section renders from it; V reads and appends to it. One file a
human can open, edit, and reason about.

Why a flat dated list rather than a note per event or a section per day: both
alternatives require inserting into the middle of a file, and the vault writer
is append-only for a reason. A flat list sorts at render time and appends at
write time, so adding an event stays an ordinary `[bot]`-committed append.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from . import notes, provenance
from .config import Settings, settings as default_settings

FOLDER = "01 - Daily Notes/Calendar"
HEADING = "Events"
DAILY_HEADING = "📅 Calendar"

# - 2026-08-08 12:30-13:30 · Summary @Location
# - 2026-08-08 12:30 · Summary
# - 2026-08-08 all-day · Summary
LINE_RE = re.compile(
    r"^\s*[-*+]\s+(\d{4}-\d{2}-\d{2})\s+"
    r"(all[- ]?day|\d{1,2}:\d{2}(?:\s*[-–—]\s*\d{1,2}:\d{2})?)"
    r"\s*(?:[·:|-]\s*)?(.*)$",
    re.I)

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass
class Event:
    date: dt.date
    start: dt.time | None          # None => all-day
    end: dt.time | None
    summary: str
    location: str = ""
    source: str = "vault"          # vault | ical

    @property
    def all_day(self) -> bool:
        return self.start is None

    @property
    def time_label(self) -> str:
        if self.all_day:
            return "all-day"
        s = self.start.strftime("%H:%M")
        return f"{s}-{self.end.strftime('%H:%M')}" if self.end else s

    def key(self) -> tuple:
        """Identity for dedup on import — a feed re-read must not double up."""
        return (self.date, self.time_label, self.summary.strip().lower())

    def line(self) -> str:
        where = f" @{self.location}" if self.location else ""
        return f"- {self.date.isoformat()} {self.time_label} · {self.summary}{where}"

    def render(self) -> str:
        where = f" _({self.location})_" if self.location else ""
        return f"{self.time_label} — {self.summary}{where}"

    def starts_at(self) -> dt.datetime | None:
        if self.all_day:
            return None
        return dt.datetime.combine(self.date, self.start)


def _parse_time(s: str) -> dt.time | None:
    m = TIME_RE.match(s.strip())
    if not m:
        return None
    try:
        return dt.time(int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_line(line: str) -> Event | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    day = notes.as_date(m.group(1))
    if not day:
        return None
    when, rest = m.group(2).strip().lower(), m.group(3).strip()
    start = end = None
    if not when.replace(" ", "").replace("-", "").startswith("allday"):
        parts = re.split(r"\s*[-–—]\s*", when)
        start = _parse_time(parts[0])
        if len(parts) > 1:
            end = _parse_time(parts[1])
    summary, location = rest, ""
    at = re.search(r"\s@(.+)$", rest)
    if at:
        summary, location = rest[:at.start()].strip(), at.group(1).strip()
    if not summary:
        return None
    return Event(day, start, end, summary, location)


class Calendar:
    def __init__(self, settings: Settings | None = None, vault=None):
        self.s = settings or default_settings
        self._vault = vault

    @property
    def vault(self):
        if self._vault is None:
            from .vaultfs import Vault
            self._vault = Vault(self.s)
        return self._vault

    # ---- paths ------------------------------------------------------
    def month_rel(self, day: dt.date) -> str:
        return f"{FOLDER}/{day.strftime('%Y-%m')}.md"

    def months_between(self, start: dt.date, end: dt.date) -> list[str]:
        out, cur = [], dt.date(start.year, start.month, 1)
        while cur <= end:
            out.append(self.month_rel(cur))
            cur = dt.date(cur.year + (cur.month == 12),
                          1 if cur.month == 12 else cur.month + 1, 1)
        return out

    # ---- reading ----------------------------------------------------
    def _events_in(self, rel: str) -> list[Event]:
        path = self.s.vault / rel
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ev = parse_line(line)
            if ev:
                out.append(ev)
        return out

    def all(self) -> list[Event]:
        folder = self.s.vault / FOLDER
        if not folder.exists():
            return []
        out: list[Event] = []
        for p in sorted(folder.glob("*.md")):
            out.extend(self._events_in(f"{FOLDER}/{p.name}"))
        return sorted(out, key=lambda e: (e.date, e.start or dt.time.min))

    def on(self, day: dt.date) -> list[Event]:
        return sorted((e for e in self._events_in(self.month_rel(day))
                       if e.date == day),
                      key=lambda e: (e.start is not None, e.start or dt.time.min))

    def between(self, start: dt.date, end: dt.date) -> list[Event]:
        out: list[Event] = []
        for rel in self.months_between(start, end):
            out.extend(e for e in self._events_in(rel) if start <= e.date <= end)
        return sorted(out, key=lambda e: (e.date, e.start or dt.time.min))

    def next_up(self, now: dt.datetime | None = None,
                within_days: int = 14) -> Event | None:
        now = now or dt.datetime.now()
        for e in self.between(now.date(), now.date() + dt.timedelta(days=within_days)):
            if e.date > now.date():
                return e
            at = e.starts_at()
            if at and at > now:
                return e
        return None

    # ---- writing ----------------------------------------------------
    def add(self, event: Event, *, dedupe: bool = True) -> str:
        rel = self.month_rel(event.date)
        if dedupe and any(e.key() == event.key() for e in self._events_in(rel)):
            return f"already on the calendar: {event.render()}"
        if not (self.s.vault / rel).exists():
            self._create_month(event.date)
        self.vault.append(
            rel, event.line(), heading=HEADING,
            summary=f"calendar: {event.date.isoformat()} {event.summary[:50]}")
        return event.render()

    def _create_month(self, day: dt.date) -> None:
        rel = self.month_rel(day)
        title = day.strftime("%B %Y")
        body = (
            f"\n# Calendar — {title}\n\n"
            "The single source of truth for events this month. One line per\n"
            "event; daily notes render their Calendar section from here, and\n"
            "V reads and appends here. Format:\n\n"
            "`- YYYY-MM-DD HH:MM-HH:MM · Summary @Location`  (or `all-day`)\n\n"
            f"## {HEADING}\n\n"
        )
        self.vault.create(rel, {
            "type": "calendar",
            "month": day.strftime("%Y-%m"),
            "month-start": dt.date(day.year, day.month, 1).isoformat(),
            "tags": ["calendar"],
        }, body, summary=f"create calendar for {title}")

    # ---- import from the remote feed --------------------------------
    def import_ical(self, start: dt.date, days: int = 30) -> tuple[int, int]:
        """Pull the iCal feed into the vault so there is one source, not two.

        Returns (added, skipped). Safe to re-run: dedup is on
        (date, time, summary), so a feed read twice adds nothing.

        Each day is isolated. One day's fetch failing used to abort the loop
        with the counters unassigned, so a run that had already written three
        events to the vault reported importing none — work done, silently
        denied. A partial import is a normal outcome for a network call and
        has to be reportable.
        """
        from .agenda import Agenda
        ag = Agenda(self.s)
        if not ag.configured():
            return (0, 0)
        added = skipped = 0
        for i in range(days):
            day = start + dt.timedelta(days=i)
            try:
                raws = ag.events(day, use_cache=False)
            except Exception:
                continue
            for raw in raws:
                ev = _from_agenda(raw, day)
                if ev is None:
                    continue
                try:
                    before = len(self._events_in(self.month_rel(day)))
                    self.add(ev)
                    after = len(self._events_in(self.month_rel(day)))
                except Exception:
                    continue
                if after > before:
                    added += 1
                else:
                    skipped += 1
        return (added, skipped)

    # ---- rendering into daily notes ---------------------------------
    def render_day(self, day: dt.date) -> str:
        evs = self.on(day)
        if not evs:
            return "_No events scheduled._"
        return "\n".join(f"- {e.render()}" for e in evs)

    def sync_daily_note(self, day: dt.date) -> str | None:
        """Refresh a daily note's `## 📅 Calendar` section from the calendar.

        This is the one section of a daily note V rewrites, and it is marked in
        the template as auto-filled. Nothing else in the note is touched: the
        replacement is verified to leave every other line byte-identical.
        """
        rel = self._daily_rel(day)
        path = self.s.vault / rel
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        body = self.render_day(day)
        # A plain path, not a wikilink. The month file only exists once
        # something is scheduled in it, and a `[[...]]` to an absent note is a
        # broken link — syncing a quiet month created eighteen of them and the
        # nudge engine dutifully reported all eighteen.
        marker = (f"<!-- Source of truth: `{FOLDER}/{day.strftime('%Y-%m')}.md` "
                  "— edit there, not here. `v calendar --sync` refreshes "
                  "this. -->")
        updated = replace_section(content, DAILY_HEADING, f"{marker}\n{body}")
        if updated is None or updated == content:
            return None
        # A copy of the month note's lines for this day: Parsa's if every
        # line it came from is, V's if any of them is V's and unapproved.
        src = self.vault.provenance.derived(
            [f"{FOLDER}/{day.strftime('%Y-%m')}.md"], day.isoformat())
        self.vault.rewrite(rel, updated, allow_rewrite=True,
                           summary=f"sync calendar into {day.isoformat()}",
                           source=src)
        return body

    def _daily_rel(self, day: dt.date) -> str:
        return (f"01 - Daily Notes/{day.year}/{day.strftime('%m - %B')}/"
                f"{day.isoformat()}.md")


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?\b", re.I)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]
_CLOCK_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.I)
_RANGE_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*(?:-|–|—|to|till|until)\s*(\d{1,2}):(\d{2})\b", re.I)
_DURATION_RE = re.compile(r"\bfor\s+(\d+)\s*(min|minute|hour|hr|h)s?\b", re.I)

# Words that describe *when*, plus filler, stripped to leave the summary.
# Every alternative is anchored on both sides. An earlier version ended the
# group with `[a-z]*`, which made `me` match "meeting" and `a` match "Arash" —
# it deleted the two things a calendar entry is actually for.
_NOISE_WORDS = (
    "hey", "hi", "hello", "ok", "okay", "so", "please", "pls", "v",
    "remind", "reminds", "reminder", "me", "my", "then", "at", "on", "in",
    "i", "should", "shall", "will", "have", "has", "had", "got", "gotta",
    "a", "an", "the", "go", "going", "goes", "to", "for", "about", "there",
    "is", "are", "be", "am", "pm", "oclock", "o'clock",
    "tomorrow", "today", "tonight", "next", "this", "coming", "week",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
)
_NOISE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _NOISE_WORDS) + r")\b", re.I)


def parse_natural(text: str, *, today: dt.date | None = None) -> Event | None:
    """Read "on Aug 8 I have a meeting at 12:30 pm" into an Event.

    Deliberately narrow. It has to be certain about the date and the time —
    everything else it can get slightly wrong and the user will fix, but a
    meeting written to the wrong day is worse than one not written at all, so
    an unparseable date returns None and the caller asks.
    """
    today = today or dt.date.today()
    low = text.lower()

    day = None
    m = _ISO_RE.search(text)
    if m:
        day = notes.as_date(m.group(0))
    if day is None:
        m = _MONTH_DAY_RE.search(text) or _DAY_MONTH_RE.search(text)
        if m:
            a, b = m.group(1), m.group(2)
            mon, num = (MONTHS[a[:3].lower()], int(b)) if a[:3].lower() in MONTHS \
                else (MONTHS[b[:3].lower()], int(a))
            year = today.year
            try:
                day = dt.date(year, mon, num)
            except ValueError:
                return None
            # A bare month/day that already passed means next year — "Jan 3"
            # said in December is not ten months ago.
            if (day - today).days < -180:
                day = dt.date(year + 1, mon, num)
    if day is None:
        if "tomorrow" in low or "فردا" in low:
            day = today + dt.timedelta(days=1)
        elif "today" in low or "tonight" in low or "امروز" in low:
            day = today
        else:
            for i, name in enumerate(_WEEKDAYS):
                if name in low:
                    ahead = (i - today.weekday()) % 7 or 7
                    day = today + dt.timedelta(days=ahead)
                    break
    if day is None:
        return None

    start = end = None
    rng = _RANGE_RE.search(text)
    if rng:
        start = _parse_time(f"{rng.group(1)}:{rng.group(2)}")
        end = _parse_time(f"{rng.group(3)}:{rng.group(4)}")
    else:
        for cm in _CLOCK_RE.finditer(text):
            if cm.group(1):
                hh, mm = int(cm.group(1)), int(cm.group(2) or 0)
                mer = (cm.group(3) or "").lower()
                if mer == "pm" and hh < 12:
                    hh += 12
                elif mer == "am" and hh == 12:
                    hh = 0
            else:
                hh, mm = int(cm.group(4)), int(cm.group(5))
            # A year or a day-of-month can look like a clock; a real time has
            # a meridiem or a colon, which the alternation above guarantees.
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                start = dt.time(hh, mm)
                break
    if start and not end:
        dm = _DURATION_RE.search(text)
        mins = 60
        if dm:
            n, unit = int(dm.group(1)), dm.group(2).lower()
            mins = n if unit.startswith("min") else n * 60
        end = (dt.datetime.combine(day, start)
               + dt.timedelta(minutes=mins)).time()

    summary = _summary_of(text)
    return Event(day, start, end, summary or "Event", "")


def _summary_of(text: str) -> str:
    """What the event *is*, once the scheduling words are removed."""
    out = re.sub(r"[\"“”']", "", text)
    out = _RANGE_RE.sub(" ", out)
    out = _CLOCK_RE.sub(" ", out)
    out = _ISO_RE.sub(" ", out)
    out = _MONTH_DAY_RE.sub(" ", out)
    out = _DAY_MONTH_RE.sub(" ", out)
    out = _DURATION_RE.sub(" ", out)
    out = _NOISE_RE.sub(" ", out)
    out = re.sub(r"[,;:]+", " ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—.,")
    # Anything left that is one throwaway word is not a summary.
    return out if len(out) > 2 else ""


def _from_agenda(raw, day: dt.date) -> Event | None:
    """Convert an `agenda.Event` (label-based) into a calendar Event."""
    if getattr(raw, "all_day", False):
        return Event(day, None, None, raw.summary, raw.location or "", "ical")
    parts = re.split(r"\s*[-–—]\s*", raw.label)
    start = _parse_time(parts[0]) if parts else None
    end = _parse_time(parts[1]) if len(parts) > 1 else None
    if start is None:
        return None
    return Event(day, start, end, raw.summary, raw.location or "", "ical")


def replace_section(content: str, heading: str, new_body: str) -> str | None:
    """Swap one section's body, leaving every other byte alone.

    Returns None if the heading isn't present — the caller decides whether
    that's an error or a no-op. Matching is on the heading text with any
    leading emoji tolerated, because `## 📅 Calendar` and `## Calendar` are the
    same section to a human.
    """
    want = _norm_heading(heading)
    lines = content.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = notes.HEADING.match(line)
        if m and _norm_heading(m.group(2)) == want:
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = notes.HEADING.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    tail = lines[end:]
    # Preserve the blank line that separated this section from the next.
    trailing = [""] if tail else []
    out = lines[:start + 1] + new_body.splitlines() + trailing + tail
    return "\n".join(out) + ("\n" if content.endswith("\n") else "")


def _norm_heading(h: str) -> str:
    return re.sub(r"[^\w؀-ۿ ]+", "", h).strip().lower()
