"""The week's shape — where the hours actually go.

`calendar.py` answers "what is scheduled on the 19th", which is a question
about *events*: things with a name that happen once. It has no answer for the
question underneath it — "am I in class right now", "when do I next have three
free hours", "how much of this week is already spoken for" — because a class
that meets twice a week for fourteen weeks is not an event, it is a *shape*,
and writing it out as 28 event lines makes the calendar unreadable and still
does not say which of the gaps between them is free.

So this is the second layer, and it is deliberately not the first one:

    01 - Daily Notes/Weekly/2026/2026-W38.md

    ## Schedule
    - 2026-09-19 08:00-16:00 · work · Divar
    - 2026-09-19 19:00-20:30 · gym
    - 2026-09-20 10:30-12:00 · class · Compiler Design @204
    - 2026-09-20 16:00-18:00 · paper

**One schedule per week, and it is editable.** This was the whole requirement
and it rules out the obvious design. A single standing "week shape" note is
smaller, but a week where a class is cancelled and a friend has a birthday is
then either a lie or a permanent edit, and the note stops describing any real
week. Weeks are where plans actually live, so the plan lives in the week note
that already exists — the one the daily notes embed from and the monthly note
aggregates.

**Repeating blocks are a seed, not the truth.** `05 - Templates/Week Schedule.md`
holds only what repeats, in day-code form:

    - SA,MO,WE 08:00-16:00 · work · Divar
    - SU,TU 10:30-12:00 · class · Compiler Design @204

Those get stamped into a week note the moment it is created — by Templater if
Obsidian gets there first, by `ensure_week` if V does. From that instant they
are *that week's* lines: move one, delete one, add five more, and no other
week notices. Editing the seed changes future weeks and leaves past ones as
they were actually lived, which is the only behaviour that lets the archive
stay honest. V edits the seed too (`add_rule`, `remove_rule`, `move_rule`), and
a change it makes there also reaches the week notes already written, from
today on, so "MLOps moved to 13:00" is true this week and not just next.

**Free time is derived, never written.** You record commitments; the gaps are
free by definition. `free()` subtracts the week's blocks *and* the calendar's
one-off events from the waking window, so a meeting that only exists in
`Calendar/2026-09.md` still makes you busy. Nothing anywhere has to remember
to write down "free" — which is good, because nobody ever would.

**Flat list, one line per block, date on every line.** Same choice
`calendar.py` made and for the same reason: a section per day reads better but
requires inserting into the middle of a file, and the vault writer is
append-only. A flat list sorts at render time and appends at write time, so
adding a block stays an ordinary `[bot]`-committed append that `v undo`
reverts.
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import re
from dataclasses import dataclass

from . import notes
from .calendar import replace_section
from .config import Settings, settings as default_settings

FOLDER = "01 - Daily Notes/Weekly"
SEED_REL = "05 - Templates/Week Schedule.md"
HEADING = "Schedule"
DAILY_HEADING = "⏱ Schedule"

# What a block *is*, not what it's called. The vocabulary is closed because
# every consumer switches on it — `free()` decides what counts as spoken for,
# the dashboard colours by it, and V answers "am I at work?" from it. An open
# vocabulary would make all three quietly wrong the first time a typo landed.
KINDS = (
    "class",      # a lecture or lab, on the university timetable
    "work",       # Divar hours
    "commute",    # travel either way — busy, but not work
    "gym",        # a planned session; the Workout note is the record of it
    "sleep",      # bounds the day, so free time isn't reported at 04:00
    "study",      # coursework, assignments, reading for a class
    "paper",      # research writing — deliberately separate from `study`
    "personal",   # errands, family, friends, anything claimed and not work
    "event",      # a one-off with a name; what a calendar line becomes here
)

# Free time is only interesting inside the waking day. These are the fallback
# bounds, used when the week declares no `sleep` block to derive them from.
DAY_START = dt.time(8, 0)
DAY_END = dt.time(23, 0)

# Saturday-first, because the vault's week runs Sat → Fri and every weekly
# note's `week-start` is a Saturday. Values are `date.weekday()`, Mon=0.
DAY_CODES = {"SA": 5, "SU": 6, "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4}
CODE_FOR = {v: k for k, v in DAY_CODES.items()}
WEEK_ORDER = [5, 6, 0, 1, 2, 3, 4]

_TIME = r"\d{1,2}:\d{2}"
# `08:00-16:00 · work · Divar @Office`, with the label optional and the
# location optional inside it.
_TAIL = (rf"({_TIME})\s*(?:[-–—]\s*({_TIME}))?"
         r"\s*[·:|]\s*([A-Za-z][\w-]*)"
         r"\s*(?:[·:|]\s*(.*))?$")

BLOCK_RE = re.compile(rf"^\s*[-*+]\s+(\d{{4}}-\d{{2}}-\d{{2}})\s+{_TAIL}")
RULE_RE = re.compile(rf"^\s*[-*+]\s+([A-Z]{{2}}(?:\s*,\s*[A-Z]{{2}})*)\s+{_TAIL}")
_TIME_RE = re.compile(rf"^({_TIME})$")


def _parse_time(s: str) -> dt.time | None:
    if not _TIME_RE.match(s.strip()):
        return None
    h, m = s.strip().split(":")
    try:
        return dt.time(int(h), int(m))
    except ValueError:
        return None


def _split_location(label: str) -> tuple[str, str]:
    """`Compiler Design @204` -> `("Compiler Design", "204")`."""
    at = re.search(r"\s@(.+)$", label)
    if not at:
        return label.strip(), ""
    return label[:at.start()].strip(), at.group(1).strip()


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _clock(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def human_span(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    if h and m:
        return f"{h}h{m:02d}"
    return f"{h}h" if h else f"{m}m"


@dataclass
class Block:
    """One claimed stretch of a real day."""

    date: dt.date
    start: dt.time
    end: dt.time | None
    kind: str
    label: str = ""
    location: str = ""

    @property
    def time_label(self) -> str:
        s = self.start.strftime("%H:%M")
        return f"{s}-{self.end.strftime('%H:%M')}" if self.end else s

    @property
    def minutes(self) -> int:
        """Zero for a block with no end — a marker, not a claim on the day."""
        if not self.end:
            return 0
        return max(0, _minutes(self.end) - _minutes(self.start))

    @property
    def title(self) -> str:
        return self.label or self.kind

    def line(self) -> str:
        bits = [f"- {self.date.isoformat()} {self.time_label} · {self.kind}"]
        if self.label:
            bits.append(f" · {self.label}")
        if self.location:
            bits.append(f" @{self.location}")
        return "".join(bits)

    def render(self) -> str:
        where = f" _({self.location})_" if self.location else ""
        label = f" — {self.label}" if self.label else ""
        return f"{self.time_label} · **{self.kind}**{label}{where}"

    def covers(self, when: dt.time) -> bool:
        if not self.end:
            return False
        return self.start <= when < self.end

    def key(self) -> tuple:
        return (self.date, self.time_label, self.kind, self.label.strip().lower())


@dataclass
class Rule:
    """A repeating block, as written in the seed template."""

    days: tuple[int, ...]
    start: dt.time
    end: dt.time | None
    kind: str
    label: str = ""
    location: str = ""

    @property
    def time_label(self) -> str:
        s = self.start.strftime("%H:%M")
        return f"{s}-{self.end.strftime('%H:%M')}" if self.end else s

    def line(self) -> str:
        out = f"- {day_codes(self.days)} {self.time_label} · {self.kind}"
        if self.label:
            out += f" · {self.label}"
        if self.location:
            out += f" @{self.location}"
        return out

    def block_on(self, day: dt.date) -> Block:
        """This rule as the dated line it stamps into `day`'s week."""
        return Block(day, self.start, self.end, self.kind, self.label,
                     self.location)

    def expand(self, week_start: dt.date) -> list[Block]:
        """The real dated blocks this rule makes in the week starting Saturday."""
        return [self.block_on(week_start + dt.timedelta(days=offset))
                for offset, weekday in enumerate(WEEK_ORDER)
                if weekday in self.days]


def parse_block(line: str) -> Block | None:
    m = BLOCK_RE.match(line)
    if not m:
        return None
    day = notes.as_date(m.group(1))
    start = _parse_time(m.group(2))
    if not day or not start:
        return None
    end = _parse_time(m.group(3)) if m.group(3) else None
    label, location = _split_location(m.group(5) or "")
    return Block(day, start, end, m.group(4).lower(), label, location)


def parse_rule(line: str) -> Rule | None:
    m = RULE_RE.match(line)
    if not m:
        return None
    days = []
    for code in m.group(1).split(","):
        wd = DAY_CODES.get(code.strip().upper())
        if wd is None:
            return None       # one bad code invalidates the line, loudly-ish
        days.append(wd)
    start = _parse_time(m.group(2))
    if not days or not start:
        return None
    end = _parse_time(m.group(3)) if m.group(3) else None
    label, location = _split_location(m.group(5) or "")
    return Rule(tuple(days), start, end, m.group(4).lower(), label, location)


def day_codes(days) -> str:
    """`(0, 5)` → `"SA,MO"` — in week order, not weekday-number order."""
    return ",".join(CODE_FOR[d] for d in sorted(set(days), key=WEEK_ORDER.index))


def parse_days(text: str) -> tuple[int, ...] | None:
    """`"SA,MO"`, `"sat mon"`, `"saturday and monday"` → weekday numbers.

    Any word that is not a day makes the whole thing None rather than being
    skipped: "MO,WD" is a typo for a day somebody meant, and quietly dropping
    it would repeat the block on fewer days than were asked for.
    """
    out: list[int] = []
    for word in re.split(r"[\s,/&+]+", (text or "").strip()):
        if not word or word.lower() == "and":
            continue
        wd = DAY_CODES.get(word.upper(), _WEEKDAY_WORDS.get(word.lower()))
        if wd is None:
            return None
        if wd not in out:
            out.append(wd)
    return tuple(sorted(out, key=WEEK_ORDER.index)) or None


@dataclass
class Gap:
    """A stretch of a day nothing has claimed."""

    date: dt.date
    start: dt.time
    end: dt.time

    @property
    def minutes(self) -> int:
        return max(0, _minutes(self.end) - _minutes(self.start))

    def render(self) -> str:
        return (f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} "
                f"({human_span(self.minutes)})")


def week_start_of(day: dt.date) -> dt.date:
    """The Saturday that opens `day`'s week."""
    return day - dt.timedelta(days=(day.weekday() + 2) % 7)


def week_label(day: dt.date) -> str:
    """`2026-W38` — ISO week of the *Saturday*, matching the vault's template."""
    sat = week_start_of(day)
    iso_year, iso_week, _ = sat.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


class WeekPlan:
    def __init__(self, settings: Settings | None = None, vault=None,
                 calendar=None):
        self.s = settings or default_settings
        self._vault = vault
        self._calendar = calendar

    @property
    def vault(self):
        if self._vault is None:
            from .vaultfs import Vault
            self._vault = Vault(self.s)
        return self._vault

    @property
    def calendar(self):
        if self._calendar is None:
            from .calendar import Calendar
            self._calendar = Calendar(self.s, vault=self._vault)
        return self._calendar

    # ---- paths ------------------------------------------------------
    def week_rel(self, day: dt.date) -> str:
        sat = week_start_of(day)
        return f"{FOLDER}/{sat.strftime('%Y')}/{week_label(sat)}.md"

    def _daily_rel(self, day: dt.date) -> str:
        return (f"01 - Daily Notes/{day.year}/{day.strftime('%m - %B')}/"
                f"{day.isoformat()}.md")

    # ---- the seed ---------------------------------------------------
    def rules(self) -> list[Rule]:
        """What repeats, from `05 - Templates/Week Schedule.md`.

        A missing seed is not an error — it means nothing repeats yet, which is
        the correct state for a vault the day before a semester starts.
        """
        return [rule for _, rule in self._seed_lines()]

    def _seed_lines(self) -> list[tuple[str, Rule]]:
        """Every rule, paired with the exact line it is written as.

        The written line is what an edit has to match, and it is not always
        `rule.line()`: a hand-written `- MO,SA 9:00-10:30 · class` parses to
        the same rule as the canonical form but is not the same text.
        """
        path = self.s.vault / SEED_REL
        if not path.exists():
            return []
        out, fenced = [], False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            # A fenced block is documentation, not schedule. The file has to
            # be able to *show* the format it wants without the example
            # becoming a rule that gets stamped into a real week — which is
            # exactly how "work · Divar 08:00-16:00" ended up in a live note.
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            rule = parse_rule(line)
            if rule:
                out.append((line, rule))
        return out

    def seed_blocks(self, day: dt.date) -> list[Block]:
        """Every repeating block, expanded onto the week containing `day`."""
        sat = week_start_of(day)
        out: list[Block] = []
        for rule in self.rules():
            out.extend(rule.expand(sat))
        return sort_blocks(out)

    def render_section(self, day: dt.date) -> str:
        """The `## Schedule` body a fresh week note starts life with."""
        blocks = self.seed_blocks(day)
        marker = (f"<!-- Repeating blocks come from `{SEED_REL}`. Edit here for "
                  "this week only; edit the template to change every week from "
                  "now on. -->")
        if not blocks:
            return (f"{marker}\n_Nothing scheduled yet — add lines like_ "
                    "`- 2026-09-19 10:30-12:00 · class · Compiler @204`")
        return marker + "\n" + "\n".join(b.line() for b in blocks)

    # ---- reading ----------------------------------------------------
    def _blocks_in(self, rel: str) -> list[Block]:
        path = self.s.vault / rel
        if not path.exists():
            return []
        section = notes.parse(path, self.s.vault).section(HEADING)
        if section is None:
            return []
        out = []
        for line in section.splitlines():
            block = parse_block(line)
            if block:
                out.append(block)
        return out

    def week(self, day: dt.date) -> list[Block]:
        """Every block in `day`'s week, as written in that week's note."""
        return sort_blocks(self._blocks_in(self.week_rel(day)))

    def on(self, day: dt.date) -> list[Block]:
        return [b for b in self.week(day) if b.date == day]

    def busy(self, day: dt.date, *, include_events: bool = True) -> list[Block]:
        """Everything that claims part of `day` — blocks plus calendar events.

        A one-off meeting lives in `Calendar/YYYY-MM.md` and never gets copied
        here, so free time computed from the week note alone would cheerfully
        offer you an hour you are already in a meeting for. Events arrive as
        `event` blocks, which is what they are once the question is "is this
        hour spoken for" rather than "what is it called".
        """
        out = list(self.on(day))
        if include_events:
            # Dedup on what the thing is *called*, not on its kind: a class
            # that the Google import also dropped into the month calendar is
            # one obligation, and listing it as both `class` and `event` would
            # double-count the hours the dashboard reports.
            seen = {(b.date, b.time_label, b.title.strip().lower()) for b in out}
            for ev in self.calendar.on(day):
                if ev.all_day or not ev.start:
                    continue
                block = Block(ev.date, ev.start, ev.end, "event",
                              ev.summary, ev.location)
                if (block.date, block.time_label,
                        block.title.strip().lower()) not in seen:
                    out.append(block)
        return sort_blocks(out)

    def bounds(self, day: dt.date) -> tuple[dt.time, dt.time]:
        """The waking window, derived from `sleep` blocks when there are any.

        A sleep block that ends in the morning opens the day; one that starts
        in the evening closes it. Without either, the module constants stand in
        — the alternative is reporting eight free hours at four in the morning.
        """
        start, end = DAY_START, DAY_END
        for b in self.on(day):
            if b.kind != "sleep" or not b.end:
                continue
            if b.end <= dt.time(12, 0):
                start = max(start, b.end)
            if b.start >= dt.time(12, 0):
                end = min(end, b.start)
        return (start, end) if _minutes(start) < _minutes(end) else (DAY_START, DAY_END)

    def free(self, day: dt.date, *, min_minutes: int = 30,
             after: dt.time | None = None) -> list[Gap]:
        """The gaps. This is the whole point of the module.

        Overlapping blocks are merged before subtracting, so a class that
        happens to sit inside a work block does not carve the day into a false
        sliver between them.
        """
        lo, hi = self.bounds(day)
        if after and _minutes(after) > _minutes(lo):
            lo = after
        cursor = _minutes(lo)
        limit = _minutes(hi)
        out: list[Gap] = []
        for s, e in merge_spans(self.busy(day)):
            if e <= cursor:
                continue
            if s >= limit:
                break
            if s - cursor >= min_minutes:
                out.append(Gap(day, _time(cursor), _time(s)))
            cursor = max(cursor, e)
        if limit - cursor >= min_minutes:
            out.append(Gap(day, _time(cursor), _time(limit)))
        return out

    def now_block(self, when: dt.datetime | None = None) -> Block | None:
        """What you are in the middle of, or None if the hour is your own."""
        when = when or dt.datetime.now()
        for b in self.busy(when.date()):
            if b.covers(when.time()):
                return b
        return None

    def next_free(self, minutes: int, *, start: dt.datetime | None = None,
                  days: int = 7) -> Gap | None:
        """The first gap at least `minutes` long, searching forward."""
        start = start or dt.datetime.now()
        for i in range(days):
            day = start.date() + dt.timedelta(days=i)
            after = start.time() if i == 0 else None
            for gap in self.free(day, min_minutes=minutes, after=after):
                return gap
        return None

    def load(self, day: dt.date) -> dict[str, int]:
        """Minutes claimed per kind across `day`'s week. Feeds the dashboard."""
        out: dict[str, int] = {}
        for b in self.week(day):
            out[b.kind] = out.get(b.kind, 0) + b.minutes
        return out

    # ---- writing ----------------------------------------------------
    def ensure_week(self, day: dt.date) -> str:
        """Create `day`'s weekly note, seeded with what repeats, if absent.

        Mirrors `capture.ensure_daily` down to the reason: on a normal week
        Templater's own template makes the note and this never runs, so the
        body has to come from `05 - Templates/Weekly Note.md` rather than from
        a heading set written out here, or a week V got to first would be
        visibly poorer than every other week.
        """
        from . import templates
        rel = self.week_rel(day)
        if self.vault.exists(rel):
            return rel
        sat = week_start_of(day)
        label = week_label(sat)
        meta = {
            "week-start": sat.isoformat(),
            "week": label,
            "year": sat.strftime("%Y"),
            "quarter": f"{sat.year}-Q{(sat.month - 1) // 3 + 1}",
            "tags": ["weekly-note"],
        }
        # The weekly template addresses its dates by bare name — `<% saturday %>`,
        # `<% weekLabel %>` — because Templater's own execution block defines
        # them as consts. That block does not run here, so the names have to be
        # supplied, or the note reads "# Week of  (Sat → Fri)".
        context = {
            "saturday": sat.isoformat(),
            "weekLabel": label,
            "year": sat.strftime("%Y"),
            "quarter": str((sat.month - 1) // 3 + 1),
            "heading": f"{sat.strftime('%B')} {sat.day}, {sat.year}",
        }
        filled = templates.fill("weekly", meta, title=label, when=sat,
                                context=context, settings=self.s)
        if filled:
            meta, body = filled
        else:
            body = (f"\n# Week of {sat.strftime('%B %-d, %Y')} (Sat → Fri)\n\n"
                    "## Weekly Focus\n\n")
        if not _has_heading(body, HEADING):
            body = body.rstrip("\n") + f"\n\n## {HEADING}\n\n"
        updated = replace_section(body, HEADING, self.render_section(sat))
        self.vault.create(rel, meta, updated or body,
                          summary=f"scaffold weekly note {label}")
        return rel

    def add(self, block: Block, *, dedupe: bool = True) -> str:
        """Append one block to its week, creating the week note if needed."""
        rel = self.week_rel(block.date)
        if not self.vault.exists(rel):
            self.ensure_week(block.date)
        if dedupe and any(b.key() == block.key() for b in self._blocks_in(rel)):
            return f"already on the schedule: {block.render()}"
        self.vault.append(
            rel, block.line(), heading=HEADING,
            summary=f"schedule: {block.date.isoformat()} {block.title[:50]}")
        return block.render()

    # ---- changing a block that is already there ---------------------
    #
    # These three are the reason `Persona.edit_surfaces` is not the whole
    # story. A cancelled class, a meeting pushed an hour, a gym session moved
    # to Tuesday — all of them are *edits*, and none of them is the kind of
    # edit `vault_rewrite` is dangerous for: each one names one line, in one
    # section, of one note, in a grammar that is checked before anything is
    # written. `vault.edit` refuses a line it cannot find exactly once, so a
    # stale view of the week fails loudly instead of deleting a neighbour.

    def find(self, day: dt.date, line: str) -> Block | None:
        """The block whose written line is exactly `line`, on `day`'s week."""
        want = line.strip()
        for b in self.week(day):
            if b.line().strip() == want:
                return b
        return None

    def remove(self, day: dt.date, line: str) -> str:
        """Delete one block line from its week note."""
        rel = self.week_rel(day)
        block = self.find(day, line)
        if block is None:
            raise ValueError(
                f"no such block in {rel}: {line.strip()!r} — re-read the week; "
                "you are working from a version of it that isn't there.")
        written = block.line()
        # The newline goes with it, or the section grows a blank line every
        # time something is cancelled.
        try:
            self.vault.edit(rel, written + "\n", "",
                            summary=f"schedule: drop {block.date} {block.title[:40]}")
        except Exception:
            self.vault.edit(rel, written, "",
                            summary=f"schedule: drop {block.date} {block.title[:40]}")
        return f"removed · {block.render()}"

    def replace(self, day: dt.date, line: str, new: Block) -> str:
        """Swap one block line for another, in place.

        In place matters: a remove-then-append would move the line to the
        bottom of the section, so a note read in Obsidian would slowly stop
        being in any order at all.
        """
        rel = self.week_rel(day)
        block = self.find(day, line)
        if block is None:
            raise ValueError(
                f"no such block in {rel}: {line.strip()!r} — re-read the week.")
        if new.date != block.date and self.week_rel(new.date) != rel:
            # Moving to a different week is two writes on two notes, not one
            # edit — do it as such, so neither half can half-happen quietly.
            self.remove(day, line)
            return self.add(new)
        if new.line() == block.line():
            return f"unchanged · {block.render()}"
        self.vault.edit(rel, block.line(), new.line(),
                        summary=f"schedule: {new.date} {new.title[:40]}")
        return f"moved · {new.render()}"

    def move(self, day: dt.date, line: str, *, date: dt.date | None = None,
             start: dt.time | None = None, end: dt.time | None = None,
             kind: str | None = None, label: str | None = None,
             location: str | None = None) -> str:
        """Change whichever fields are named and leave the rest alone."""
        block = self.find(day, line)
        if block is None:
            raise ValueError(
                f"no such block in {self.week_rel(day)}: {line.strip()!r}")
        if kind is not None and kind.lower() not in KINDS:
            raise ValueError(
                f"{kind!r} is not a kind — use one of: {', '.join(KINDS)}")
        new = Block(
            date=date or block.date,
            start=start or block.start,
            end=end if end is not None else block.end,
            kind=(kind or block.kind).lower(),
            label=block.label if label is None else label,
            location=block.location if location is None else location)
        if new.end and _minutes(new.end) <= _minutes(new.start):
            raise ValueError(
                f"{new.time_label} ends before it starts — a block that "
                "crosses midnight has to be written as two.")
        return self.replace(day, line, new)

    def reseed(self, day: dt.date) -> int:
        """Add any repeating block missing from `day`'s week. Returns the count.

        For the week you edited before the timetable changed, and for the week
        Templater made before the seed existed. Append-only and deduped, so it
        is safe to run twice and it never removes a line you wrote by hand.
        """
        rel = self.week_rel(day)
        if not self.vault.exists(rel):
            self.ensure_week(day)
            return len(self.seed_blocks(day))
        have = {b.key() for b in self._blocks_in(rel)}
        added = 0
        for block in self.seed_blocks(day):
            if block.key() not in have:
                self.add(block, dedupe=False)
                added += 1
        return added

    # ---- changing what repeats --------------------------------------
    #
    # Until these existed the seed was the one part of the schedule V could
    # read but not correct: the block verbs above stop at the week note, and
    # their tool descriptions sent Parsa off to edit the template by hand.
    # These change it on the same terms as the block verbs — a rule is named
    # by a day and a start time, the kind is checked before anything is
    # written, and `vault.edit` refuses a line it cannot find exactly once.
    #
    # A change to the seed also reaches the week notes that already exist,
    # from today on. Without that, "MLOps moved to 13:00" would change every
    # week except the one being lived, which is the one he was talking about.
    # Days already lived are never touched, and a week line that no longer
    # matches the rule — moved or renamed there by hand — is left alone and
    # reported, because that was a decision about that one week.

    def find_rule(self, weekday: int, start: dt.time) -> tuple[str, Rule]:
        """The one rule repeating on `weekday` at `start`, with its written line."""
        seed = self._seed_lines()
        hits = [(raw, r) for raw, r in seed
                if weekday in r.days and r.start == start]
        if len(hits) == 1:
            return hits[0]
        code = CODE_FOR[weekday]
        if not hits:
            listed = ", ".join(f"{r.time_label} {r.label or r.kind}"
                               for _, r in seed if weekday in r.days)
            raise ValueError(f"nothing repeats at {start:%H:%M} on {code}. "
                             f"{code} has: {listed or 'nothing'}.")
        listed = ", ".join(f"{r.kind} {r.label}".strip() for _, r in hits)
        raise ValueError(f"{len(hits)} repeating blocks start at {start:%H:%M} "
                         f"on {code} ({listed}) — can't tell which you mean.")

    def add_rule(self, rule: Rule, *, today: dt.date | None = None) -> str:
        """Make a block repeat every week, starting with this one."""
        _check_rule(rule)
        if any(r.line() == rule.line() for r in self.rules()):
            return f"already repeats · {rule.line()[2:]}"
        summary = f"repeat: {rule.line()[2:62]}"
        if self.vault.exists(SEED_REL):
            self.vault.append(SEED_REL, rule.line(), summary=summary)
        else:
            self.vault.create(SEED_REL, dict(SEED_META),
                              _seed_body() + rule.line() + "\n", summary=summary)
        changed = []
        for d in self._upcoming(rule.days, today):
            if self._lacks(rule.block_on(d)):
                self.add(rule.block_on(d), dedupe=False)
                changed.append(d)
        return self._finish(f"repeats · {rule.line()[2:]}", changed, [])

    def remove_rule(self, weekday: int, start: dt.time, *,
                    all_days: bool = False,
                    today: dt.date | None = None) -> str:
        """Stop a block repeating — on `weekday` only, or on every day it does."""
        raw, rule = self.find_rule(weekday, start)
        gone = rule.days if all_days else (weekday,)
        rest = tuple(d for d in rule.days if d not in gone)
        self._rewrite_rule(
            raw, dataclasses.replace(rule, days=rest).line() if rest else "",
            summary=f"stop repeating: {day_codes(gone)} {rule.label or rule.kind}"[:70])
        changed, kept = [], []
        for d in self._upcoming(gone, today):
            line = rule.block_on(d).line()
            if self.find(d, line) is None:
                kept.append(d)
                continue
            self.remove(d, line)
            changed.append(d)
        head = (f"no longer repeats on {day_codes(gone)} · "
                f"{rule.time_label} · {rule.kind}"
                + (f" · {rule.label}" if rule.label else ""))
        return self._finish(head, changed, kept)

    def move_rule(self, weekday: int, start: dt.time, *,
                  all_days: bool = False,
                  days: tuple[int, ...] | None = None,
                  to_start: dt.time | None = None, end: dt.time | None = None,
                  kind: str | None = None, label: str | None = None,
                  location: str | None = None,
                  today: dt.date | None = None) -> str:
        """Change a repeating block. Only what is named changes.

        Without `all_days` only `weekday`'s occurrence moves, and a rule that
        repeats on other days is split in two: "Monday's MLOps is at 13:00
        now" must not drag Saturday's along with it.
        """
        raw, rule = self.find_rule(weekday, start)
        moving = rule.days if all_days else (weekday,)
        rest = tuple(d for d in rule.days if d not in moving)
        new = Rule(
            days=tuple(days or moving),
            start=to_start or rule.start,
            end=end if end is not None else rule.end,
            kind=(kind or rule.kind).lower(),
            label=rule.label if label is None else label,
            location=rule.location if location is None else location)
        _check_rule(new)
        if not rest and new.line() == rule.line():
            return f"unchanged · {rule.line()[2:]}"
        # The days left behind keep the line's place and the moved part goes
        # directly under it, so the seed reads in the order it was written.
        lines = ([dataclasses.replace(rule, days=rest).line()] if rest else [])
        lines.append(new.line())
        self._rewrite_rule(raw, "\n".join(lines),
                           summary=f"repeat: {new.line()[2:62]}")

        changed, kept = [], []
        same_days = set(new.days) == set(moving)
        for d in self._upcoming(moving, today):
            old = rule.block_on(d).line()
            if self.find(d, old) is None:
                kept.append(d)
                continue
            if same_days:
                self.replace(d, old, new.block_on(d))
            else:
                self.remove(d, old)
            changed.append(d)
        if not same_days:
            for d in self._upcoming(new.days, today):
                if self._lacks(new.block_on(d)):
                    self.add(new.block_on(d), dedupe=False)
                    changed.append(d)
        head = "repeats now · " + " · and ".join(l[2:] for l in lines)
        return self._finish(head, sorted(set(changed)), kept)

    def _upcoming(self, days, today: dt.date | None) -> list[dt.date]:
        """Dates on `days`, today onward, inside week notes that already exist.

        Weeks with no note need nothing: they are stamped from the updated
        seed the moment they are made. A year out is the horizon — no
        timetable is planned further than that, and a week note made that far
        ahead was made by mistake.
        """
        today = today or dt.date.today()
        sat = week_start_of(today)
        out = []
        for w in range(53):
            first = sat + dt.timedelta(weeks=w)
            if not self.vault.exists(self.week_rel(first)):
                continue
            out.extend(d for d in (first + dt.timedelta(i) for i in range(7))
                       if d >= today and d.weekday() in days)
        return out

    def _lacks(self, block: Block) -> bool:
        return not any(b.key() == block.key() for b in self.on(block.date))

    def _rewrite_rule(self, raw: str, new: str, *, summary: str) -> None:
        if new:
            self.vault.edit(SEED_REL, raw, new, summary=summary)
            return
        # The newline goes with a deleted line, as in `remove`.
        try:
            self.vault.edit(SEED_REL, raw + "\n", "", summary=summary)
        except Exception:
            self.vault.edit(SEED_REL, raw, "", summary=summary)

    def _finish(self, head: str, changed: list[dt.date],
                kept: list[dt.date]) -> str:
        """Report a seed change, and refresh the daily notes it reached."""
        for d in changed:
            self.sync_daily_note(d)
        out = head
        if changed:
            out += ("\nalso changed in the weeks already written: "
                    + ", ".join(d.isoformat() for d in changed))
        if kept:
            out += ("\nleft alone, because that week's line was already "
                    "changed by hand: " + ", ".join(d.isoformat() for d in kept))
        return out

    # ---- rendering into daily notes ---------------------------------
    def render_day(self, day: dt.date) -> str:
        blocks = self.busy(day)
        if not blocks:
            return "_Nothing scheduled._"
        lines = [f"- {b.render()}" for b in blocks]
        gaps = self.free(day, min_minutes=45)
        if gaps:
            free = ", ".join(g.render() for g in gaps)
            total = human_span(sum(g.minutes for g in gaps))
            lines.append(f"\n**Free ({total}):** {free}")
        return "\n".join(lines)

    def sync_daily_note(self, day: dt.date) -> str | None:
        """Refresh a daily note's `## ⏱ Schedule` section from the week note.

        Same contract as `calendar.sync_daily_note`: one section, marked in the
        template as auto-filled, and every other byte left alone.
        """
        rel = self._daily_rel(day)
        path = self.s.vault / rel
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        body = self.render_day(day)
        marker = (f"<!-- Source of truth: `{self.week_rel(day)}` — edit there, "
                  "not here. `v week --sync` refreshes this. -->")
        updated = replace_section(content, DAILY_HEADING, f"{marker}\n{body}")
        if updated is None:
            # The section isn't there — which is every daily note written
            # before this feature existed, i.e. all of them. Refusing would
            # make the schedule invisible until the next new note, so it is
            # added, once, directly after the Calendar section. Still a single
            # `[bot]` commit, still one `v undo` away.
            updated = _insert_section(content, DAILY_HEADING,
                                      f"{marker}\n{body}")
        if updated is None or updated == content:
            return None
        self.vault.rewrite(rel, updated, allow_rewrite=True,
                           summary=f"sync schedule into {day.isoformat()}")
        return body


# ---- helpers ---------------------------------------------------------- #

def _time(minutes: int) -> dt.time:
    return dt.time(min(23, minutes // 60), minutes % 60)


def _insert_section(content: str, heading: str, body: str) -> str | None:
    """Add a `## heading` section, after the Calendar section where there is one.

    Events, then the shape of the day they sit in — the same order the daily
    template puts them in, so a note that gains the section late still reads
    like one that always had it.
    """
    section = f"## {heading}\n{body}\n"
    lines = content.splitlines()
    anchors = ("## Tasks", "## Work Log")
    for i, line in enumerate(lines):
        if line.strip() in anchors:
            out = lines[:i] + section.splitlines() + [""] + lines[i:]
            return "\n".join(out) + ("\n" if content.endswith("\n") else "")
    return content.rstrip("\n") + "\n\n" + section


def _has_heading(body: str, heading: str) -> bool:
    want = heading.strip().lower()
    return any(m.group(2).strip().lower() == want
               for m in notes.HEADING.finditer(body))


def sort_blocks(blocks: list[Block]) -> list[Block]:
    return sorted(blocks, key=lambda b: (b.date, b.start, b.kind))


def merge_spans(blocks: list[Block]) -> list[tuple[int, int]]:
    """Blocks → non-overlapping (start, end) minute pairs, in order."""
    spans = sorted((_minutes(b.start), _minutes(b.end))
                   for b in blocks if b.end and b.minutes > 0)
    out: list[tuple[int, int]] = []
    for s, e in spans:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def parse_natural(text: str, *, today: dt.date | None = None) -> Block | None:
    """`"class compiler tuesday 10:30-12:00"` → a Block, or None.

    Deliberately small: a day code or weekday name, a time span, a kind from
    the vocabulary, and whatever is left over as the label. Anything more
    ambiguous than that is a job for the agent, which can just call `add`.
    """
    today = today or dt.date.today()
    raw = text.strip()

    span = re.search(rf"({_TIME})\s*(?:[-–—]\s*({_TIME}))?", raw)
    if not span:
        return None
    start = _parse_time(span.group(1))
    if not start:
        return None
    end = _parse_time(span.group(2)) if span.group(2) else None
    rest = (raw[:span.start()] + " " + raw[span.end():]).strip()

    day = notes.as_date(rest)
    if day is None:
        day = _weekday_in(rest, today)
    if day is None:
        day = today
    rest = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", rest)

    kind = "personal"
    words = []
    for word in rest.split():
        bare = word.strip(",.;:").lower()
        if bare in _WEEKDAY_WORDS or bare in ("on", "at", "from", "to", "this", "next"):
            continue
        if kind == "personal" and bare in KINDS:
            kind = bare
            continue
        words.append(word)
    label, location = _split_location(" ".join(words).strip())
    return Block(day, start, end, kind, label, location)


_WEEKDAY_WORDS = {
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6, "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
}


def _weekday_in(text: str, today: dt.date) -> dt.date | None:
    """The next occurrence of a weekday named in `text`, today included."""
    for word in re.findall(r"[A-Za-z]+", text.lower()):
        wd = _WEEKDAY_WORDS.get(word)
        if wd is None:
            continue
        ahead = (wd - today.weekday()) % 7
        return today + dt.timedelta(days=ahead)
    return None


# ---- installing the vault side ---------------------------------------- #
# Four files, and V writes all four rather than shipping them as a patch to
# apply by hand: every one is a vault mutation, so every one should be a
# `[bot]` commit that `v undo` reverts. `v week --init` is idempotent — it
# writes only what is missing, so running it twice changes nothing.

SEED_TEMPLATE = """---
type: week-schedule
tags:
  - schedule
---

# Week Schedule

The blocks that **repeat** — classes, work hours, the gym. This file is a seed,
not a record: these lines get stamped into each new weekly note's
`## Schedule`, and from that moment they belong to that week. Edit a week note
to change one week; edit this file to change every week from now on. Past
weeks are never touched either way.

Format — `- DAYS HH:MM-HH:MM · kind · label @location`

- days are `SA SU MO TU WE TH FR`, comma-separated; the week runs Sat → Fri
- kind is one of: {kinds}
- `· label` and `@location` are both optional

```
- SA,MO,WE 08:00-16:00 · work · Divar
- SU,TU 10:30-12:00 · class · Compiler Design @204
- SA,MO,WE 19:00-20:30 · gym
```

The block above is an example and is **ignored** — fenced code is
documentation, not schedule. Write your real ones below it.

"""
SEED_META = {"type": "week-schedule", "tags": ["schedule"]}


def _seed_body() -> str:
    """The seed's body with no rules yet — `install`, or V's first `add_rule`."""
    return SEED_TEMPLATE.split("---\n", 2)[2].format(
        kinds=" · ".join(f"`{k}`" for k in KINDS))


def _check_rule(rule: Rule) -> None:
    """Refuse a rule before it is written, not after it is stamped into weeks."""
    if not rule.days:
        raise ValueError("a repeating block needs at least one day")
    if rule.kind not in KINDS:
        raise ValueError(
            f"{rule.kind!r} is not a kind — use one of: {', '.join(KINDS)}")
    if rule.end and _minutes(rule.end) <= _minutes(rule.start):
        raise ValueError(
            f"{rule.time_label} ends before it starts — a block that crosses "
            "midnight has to be written as two.")
    if any("\n" in s for s in (rule.label, rule.location)):
        raise ValueError("a label or location can't span lines")


# Templater's half of "both". It reads the same seed file this module reads
# and expands it with the same day codes, so a week note made in Obsidian and
# one made by V come out identical — the failure `templates.py` was written to
# stop, happening one layer up.
WEEKLY_SECTION = """## Schedule

<%*
const _seedPath = "05 - Templates/Week Schedule.md";
const _off = {SA: 0, SU: 1, MO: 2, TU: 3, WE: 4, TH: 5, FR: 6};
const _sat = moment(saturday);
let _rows = [];
const _seed = app.vault.getAbstractFileByPath(_seedPath);
if (_seed) {
  const _raw = await app.vault.read(_seed);
  for (const _line of _raw.split("\\n")) {
    const _m = _line.match(/^\\s*[-*+]\\s+([A-Z]{2}(?:\\s*,\\s*[A-Z]{2})*)\\s+(\\d{1,2}:\\d{2}(?:\\s*-\\s*\\d{1,2}:\\d{2})?)\\s*[·:|]\\s*(.*)$/);
    if (!_m) continue;
    for (const _code of _m[1].split(",")) {
      const _d = _off[_code.trim().toUpperCase()];
      if (_d === undefined) continue;
      _rows.push([_d, _m[2], `- ${_sat.clone().add(_d, "days").format("YYYY-MM-DD")} ${_m[2]} · ${_m[3]}`]);
    }
  }
}
_rows.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1]));
tR += "<!-- Repeating blocks come from `05 - Templates/Week Schedule.md`. Edit here for this week only; edit the template to change every week from now on. -->\\n";
tR += _rows.length ? _rows.map(r => r[2]).join("\\n")
                   : "_Nothing repeating yet — see `05 - Templates/Week Schedule.md`._";
%>

"""

DAILY_SECTION = """## ⏱ Schedule
<!-- Auto-filled from this week's note: run `v week --sync` -->
_Not synced yet._

"""

DASHBOARD = """---
type: dashboard
tags:
  - schedule
---

# Schedule Dashboard

Where the hours actually go. Everything here is read from this week's note —
`01 - Daily Notes/Weekly/YYYY/YYYY-Www.md`, section `## Schedule`. Edit there,
not here.

## This week

```dataviewjs
// The grid is built as one HTML table through `dv.el` rather than `dv.table`.
// `dv.table` renders each cell as markdown, and whether inline HTML survives
// that depends on settings — which is not a thing a dashboard should be
// betting its only view on.
const KIND_COLOR = {
  class:    "#7c5cff", work:  "#2f81f7", commute: "#8b949e", gym: "#3fb950",
  sleep:    "#484f58", study: "#d29922", paper:   "#db61a2",
  personal: "#39c5cf", event: "#f0883e",
};
const DAYS = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"];
const BLOCK = /^\\s*[-*+]\\s+(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{1,2}:\\d{2})(?:\\s*-\\s*(\\d{1,2}:\\d{2}))?\\s*[·:|]\\s*([A-Za-z][\\w-]*)\\s*(?:[·:|]\\s*(.*))?$/;
const mins = t => Number(t.split(":")[0]) * 60 + Number(t.split(":")[1]);
const esc = s => s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

const today = moment();
const sat = today.clone().subtract((today.day() + 1) % 7, "days");
const rel = `01 - Daily Notes/Weekly/${sat.format("YYYY")}/${sat.format("GGGG-[W]WW")}.md`;

let raw = "";
try { raw = await dv.io.load(rel); } catch (e) { raw = ""; }

if (!raw) {
  dv.paragraph(`No week note at \\`${rel}\\` yet — run \\`v week --ensure\\`, or make it in Obsidian.`);
} else {
  const section = (raw.split(/^##\\s+Schedule\\s*$/m)[1] || "").split(/^##\\s/m)[0];
  const blocks = [];
  for (const line of section.split("\\n")) {
    const m = line.match(BLOCK);
    if (!m || !m[3]) continue;
    const col = moment(m[1], "YYYY-MM-DD").diff(sat, "days");
    if (col < 0 || col > 6) continue;
    const label = (m[5] || "").replace(/\\s@.*$/, "").trim();
    blocks.push({ col, start: mins(m[2]), end: mins(m[3]),
                  kind: m[4].toLowerCase(), label });
  }

  if (!blocks.length) {
    dv.paragraph("Week note exists, but its `## Schedule` section is empty.");
  } else {
    // Half-hour rows, bounded by the real blocks: a week of 10:30 classes
    // should not render twelve empty rows above itself.
    const lo = Math.floor(Math.min(...blocks.map(b => b.start)) / 60) * 60;
    const hi = Math.ceil(Math.max(...blocks.map(b => b.end)) / 60) * 60;
    const isToday = c => sat.clone().add(c, "days").isSame(today, "day");

    let html = '<table style="width:100%;border-collapse:collapse;font-size:.8em;table-layout:fixed">';
    html += "<thead><tr><th style=\\"width:3.2em\\"></th>";
    for (let c = 0; c < 7; c++) {
      const d = sat.clone().add(c, "days");
      html += `<th style="padding:2px 4px;text-align:left;font-weight:${isToday(c) ? 700 : 500};opacity:${isToday(c) ? 1 : .7}">${DAYS[c]} ${d.format("D")}</th>`;
    }
    html += "</tr></thead><tbody>";

    for (let t = lo; t < hi; t += 30) {
      html += "<tr>";
      html += `<td style="opacity:.5;vertical-align:top;padding-right:4px">${t % 60 === 0 ? clock(t) : ""}</td>`;
      for (let c = 0; c < 7; c++) {
        const b = blocks.find(x => x.col === c && x.start <= t && t < x.end);
        if (!b) { html += '<td style="border-top:1px solid var(--background-modifier-border);height:1.25em"></td>'; continue; }
        const color = KIND_COLOR[b.kind] || "#8b949e";
        const first = t - b.start < 30;
        const text = first ? esc(b.label || b.kind) : "";
        html += `<td style="background:${color}26;border-left:3px solid ${color};padding:0 4px;height:1.25em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(b.kind)}${b.label ? " · " + esc(b.label) : ""}">${text}</td>`;
      }
      html += "</tr>";
    }
    html += "</tbody></table>";
    dv.el("div", "").innerHTML = html;

    const total = {};
    for (const b of blocks) total[b.kind] = (total[b.kind] || 0) + (b.end - b.start);
    const claimed = Object.values(total).reduce((a, x) => a + x, 0);
    dv.header(3, "Hours by kind");
    dv.table(["kind", "hours", "share of what's claimed"],
      Object.entries(total).sort((a, b) => b[1] - a[1]).map(([k, m]) =>
        [k, (m / 60).toFixed(1), `${Math.round(m / claimed * 100)}%`]));
    dv.paragraph(`**${(claimed / 60).toFixed(1)}h** claimed of ${7 * 15}h waking — **${(7 * 15 - claimed / 60).toFixed(1)}h** unspoken for.`);
  }
}
```

## Today

```dataviewjs
const DAY_START = 8 * 60, DAY_END = 23 * 60;
const BLOCK = /^\\s*[-*+]\\s+(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{1,2}:\\d{2})(?:\\s*-\\s*(\\d{1,2}:\\d{2}))?\\s*[·:|]\\s*([A-Za-z][\\w-]*)\\s*(?:[·:|]\\s*(.*))?$/;
const EVENT = /^\\s*[-*+]\\s+(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{1,2}:\\d{2})\\s*-\\s*(\\d{1,2}:\\d{2})\\s*[·:|]\\s*(.*)$/;
const mins = t => Number(t.split(":")[0]) * 60 + Number(t.split(":")[1]);
const clock = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const span = m => m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? String(m % 60).padStart(2, "0") : ""}` : `${m}m`;

const today = moment();
const iso = today.format("YYYY-MM-DD");
const sat = today.clone().subtract((today.day() + 1) % 7, "days");
const load = async p => { try { return (await dv.io.load(p)) || ""; } catch (e) { return ""; } };

const week = await load(`01 - Daily Notes/Weekly/${sat.format("YYYY")}/${sat.format("GGGG-[W]WW")}.md`);
const cal = await load(`01 - Daily Notes/Calendar/${today.format("YYYY-MM")}.md`);

const busy = [];
for (const line of (week.split(/^##\\s+Schedule\\s*$/m)[1] || "").split(/^##\\s/m)[0].split("\\n")) {
  const m = line.match(BLOCK);
  if (m && m[1] === iso && m[3]) busy.push({ s: mins(m[2]), e: mins(m[3]), kind: m[4],
    what: (m[5] || "").replace(/\\s@.*$/, "").trim() || m[4],
    where: ((m[5] || "").match(/\\s@(.+)$/) || ["", ""])[1].trim() });
}
for (const line of cal.split("\\n")) {
  const m = line.match(EVENT);
  if (m && m[1] === iso && !busy.some(b => b.s === mins(m[2]) && b.e === mins(m[3])))
    busy.push({ s: mins(m[2]), e: mins(m[3]), kind: "event",
      what: m[4].replace(/\\s@.*$/, "").trim(),
      where: (m[4].match(/\\s@(.+)$/) || ["", ""])[1].trim() });
}
busy.sort((a, b) => a.s - b.s);

if (!busy.length) {
  dv.paragraph("**Nothing scheduled today** — the whole day is yours.");
} else {
  dv.table(["when", "kind", "what", "where"],
    busy.map(b => [`${clock(b.s)}–${clock(b.e)}`, b.kind, b.what, b.where || "—"]));

  // Free time is the gaps, merged so an overlap can't invent a sliver.
  const merged = [];
  for (const b of busy) {
    if (merged.length && b.s <= merged[merged.length - 1][1])
      merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], b.e);
    else merged.push([b.s, b.e]);
  }
  let cur = DAY_START; const gaps = [];
  for (const [s, e] of merged) {
    if (s - cur >= 30) gaps.push([cur, s]);
    cur = Math.max(cur, e);
  }
  if (DAY_END - cur >= 30) gaps.push([cur, DAY_END]);

  dv.header(3, "Free");
  dv.paragraph(gaps.length
    ? gaps.map(([s, e]) => `**${clock(s)}–${clock(e)}** (${span(e - s)})`).join(" · ") +
      `  —  ${span(gaps.reduce((a, [s, e]) => a + e - s, 0))} total`
    : "_Nothing free today._");
}
```

## Related

- [[Week Schedule]] — the blocks that repeat
- [[Home]]
"""


def install(settings: Settings | None = None, vault=None) -> list[str]:
    """Write the vault side of this feature. Idempotent; returns what it did."""
    from .vaultfs import Vault
    s = settings or default_settings
    v = vault or Vault(s)
    done: list[str] = []

    if not v.exists(SEED_REL):
        v.create(SEED_REL, dict(SEED_META), _seed_body(),
                 summary="add the week-schedule seed template")
        done.append(f"created {SEED_REL}")

    weekly = "05 - Templates/Weekly Note.md"
    if v.exists(weekly):
        raw = (s.vault / weekly).read_text(encoding="utf-8")
        if not _has_heading(raw, HEADING):
            # Before `## Weekly Review`, so the plan sits with the plan and the
            # review stays at the bottom where the week ends.
            anchor = "## Weekly Review"
            updated = (raw.replace(anchor, WEEKLY_SECTION + anchor, 1)
                       if anchor in raw else raw.rstrip("\n") + "\n\n" + WEEKLY_SECTION)
            v.rewrite(weekly, updated, allow_rewrite=True,
                      summary="add the Schedule section to the weekly template")
            done.append(f"patched {weekly}")

    daily = "05 - Templates/Daily Note.md"
    if v.exists(daily):
        raw = (s.vault / daily).read_text(encoding="utf-8")
        if not _has_heading(raw, DAILY_HEADING):
            # Directly after the Calendar section: events, then the shape of
            # the day they sit in.
            anchor = "## Tasks"
            updated = (raw.replace(anchor, DAILY_SECTION + anchor, 1)
                       if anchor in raw else raw.rstrip("\n") + "\n\n" + DAILY_SECTION)
            v.rewrite(daily, updated, allow_rewrite=True,
                      summary="add the Schedule section to the daily template")
            done.append(f"patched {daily}")

    # The dashboard is generated, like `19 - Assistant/V/` — it is queries, not
    # prose, so it is kept current rather than left at whatever version first
    # landed. Anything hand-written belongs in the week note it reads.
    dash = "00 - Home/Schedule Dashboard.md"
    body = DASHBOARD.split("---\n", 2)[2]
    meta = {"type": "dashboard", "tags": ["schedule"]}
    if not v.exists(dash):
        v.create(dash, meta, body, summary="add the schedule dashboard")
        done.append(f"created {dash}")
    else:
        want = notes.Note(rel=dash, path=v.path(dash), meta=meta,
                          body=body).render()
        if (s.vault / dash).read_text(encoding="utf-8") != want:
            v.rewrite(dash, want, allow_rewrite=True,
                      summary="refresh the schedule dashboard")
            done.append(f"refreshed {dash}")

    return done
