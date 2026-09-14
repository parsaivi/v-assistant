"""Note bodies come from `05 - Templates/`, not from Python.

Every creation path in V used to carry its own hand-written body — `ensure_daily`
built one heading set, `work.issue_note` built another, and both were copies of a
template that Obsidian owns. Copies drift: the daily scaffold here was missing
`## 📅 Calendar`, the Work/University/Life split under Tasks, `### Blockers` and
the Gym checkbox, so a note V created and a note Templater created were visibly
different files, and the one V made lost sections the dashboards read.

So: read the real template, fill its tokens, use that. The template stays the
single source of truth, and a section added in Obsidian shows up in V's notes
without anyone editing this package.

Two token dialects appear in the vault's templates and both are handled:

  Templater   `<% tp.file.title %>`, `<% tp.date.now("GGGG-[W]WW", 0, …) %>`,
              and bare `<% year %>` names filled from a caller-supplied context
  Obsidian    `{{title}}`, `{{date}}`, `{{date:YYYY-MM-DD}}`, `{{time}}`

**Unknown tokens render empty rather than being left in place.** That is not
tidiness: `schema.validate_note` reports an unrendered `{{…}}` as an *error*,
because a template token sitting in frontmatter is the single biggest source of
broken Dataview rows. A note V writes must be schema-clean the moment it lands.

`render()` returns None when the template file is absent, so callers keep their
existing fallback body. Tests build vaults in a temp dir with no templates
folder, and a missing template is never a reason to refuse a write.
"""

from __future__ import annotations

import datetime as dt
import re

from . import notes
from .config import Settings, settings as default_settings

# Purpose → file in `05 - Templates/`. Keyed by the vault's `type` value where
# one exists, so callers can pass the type they are already holding.
TEMPLATES = {
    "daily": "Daily Note.md",
    "weekly": "Weekly Note.md",
    "monthly": "Monthly Note.md",
    "issue": "Issue.md",
    "merge-request": "Merge Request.md",
    "brag": "Brag Entry.md",
    "place": "Place.md",
    "workout": "Workout.md",
    "meal": "Meal.md",
    "bodyweight": "Bodyweight.md",
    "health-profile": "Health Profile.md",
    "assignment": "Assignment.md",
    "course": "Course.md",
    "lesson": "Lesson.md",
    "sprint": "Sprint.md",
    "wiki": "Wiki.md",
    "person": "Template_Person.md",
    "goal": "Quarterly Goals.md",
    "1on1": "1on1.md",
    "sync": "Sync Meeting.md",
}

# The heading a given kind of entry belongs under, most specific first. These
# are the templates' own section names, emoji and all — V used to append CRM
# interactions under a heading called "Interactions", which no person note has,
# so every logged interaction bolted a stray `## Interactions` section onto the
# bottom of the note instead of joining the log that was already there.
HEADINGS = {
    "interaction": ("🗓 Interaction Log", "Interaction Log", "Interactions"),
    "visit": ("Visits",),
    "experience": ("Experience", "Visits"),
    "evidence": ("Evidence",),
}

TEMPLATER = re.compile(r"<%\s*(.*?)\s*%>", re.DOTALL)
# `<%* … %>` is Templater's *execution* block: JavaScript that runs for its
# side effects and outputs nothing unless it appends to `tR`. It is code, not
# a token, so the only correct rendering of one here is the empty string.
# Before this, the weekly template — which opens with a block computing the
# week's Saturday — had its `tp.date.now("d")` plucked out of the middle of the
# JavaScript and rendered as a bare `6`, leaving that digit above a second,
# unrendered frontmatter block in every weekly note V scaffolded.
TEMPLATER_EXEC = re.compile(r"<%\*.*?-?%>\n?", re.DOTALL)
OBSIDIAN = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")
TP_DATE_NOW = re.compile(r'tp\.date\.now\(\s*"([^"]*)"(?:\s*,\s*(-?\d+))?')

# Longest alternatives first — `MMMM` must win over `MM`, which must win over
# `M`. `[…]` is moment's escape for literal text and has to be consumed here
# too, or the `W` inside `[W]WW` becomes a week number.
_MOMENT = re.compile(
    r"\[([^\]]*)\]|GGGG|YYYY|MMMM|MMM|dddd|ddd|GG|YY|WW|MM|DD|HH|mm|ss|"
    r"M|D|W|Q|H|A|d")


def moment_format(when: dt.date | dt.datetime, fmt: str) -> str:
    """Format a date with moment.js tokens — the dialect the templates speak.

    Only the tokens the vault's templates actually use, plus the obvious
    neighbours. Deliberately not a moment implementation.
    """
    if not isinstance(when, dt.datetime):
        when = dt.datetime.combine(when, dt.time())
    iso_year, iso_week, iso_day = when.isocalendar()
    table = {
        "GGGG": f"{iso_year:04d}", "GG": f"{iso_year % 100:02d}",
        "WW": f"{iso_week:02d}", "W": str(iso_week),
        "YYYY": when.strftime("%Y"), "YY": when.strftime("%y"),
        "MMMM": when.strftime("%B"), "MMM": when.strftime("%b"),
        "MM": when.strftime("%m"), "M": str(when.month),
        "DD": when.strftime("%d"), "D": str(when.day),
        "dddd": when.strftime("%A"), "ddd": when.strftime("%a"),
        "d": str(iso_day % 7),
        "Q": str((when.month - 1) // 3 + 1),
        "HH": when.strftime("%H"), "H": str(when.hour),
        "mm": when.strftime("%M"), "ss": when.strftime("%S"),
        "A": when.strftime("%p"),
    }

    def sub(m: re.Match) -> str:
        if m.group(1) is not None:      # a [literal] block
            return m.group(1)
        return table[m.group(0)]

    return _MOMENT.sub(sub, fmt)


def _lookup(context: dict, key: str) -> str:
    value = context.get(key.strip())
    return "" if value is None else str(value)


def render_text(raw: str, *, title: str = "", when: dt.date | None = None,
                context: dict | None = None) -> str:
    """Substitute every token in a template's raw text."""
    when = when or dt.date.today()
    ctx = {"title": title, **(context or {})}

    def templater(m: re.Match) -> str:
        expr = m.group(1)
        if expr.startswith("tp.file.title"):
            return title
        found = TP_DATE_NOW.search(expr)
        if found:
            fmt = found.group(1)
            offset = int(found.group(2) or 0)
            # `tp.date.now(fmt, 0, tp.file.title, "YYYY-MM-DD")` means "format
            # the date *in the filename*", not today. Daily notes depend on it:
            # scaffolding 2026-08-01 on the 3rd must write that note's own week,
            # not this one's, or it embeds the wrong weekly focus.
            ref = when
            if "tp.file.title" in expr:
                ref = notes.as_date(title) or when
            return moment_format(ref + dt.timedelta(days=offset), fmt)
        return _lookup(ctx, expr)

    def obsidian(m: re.Match) -> str:
        key, _, fmt = m.group(1).partition(":")
        key = key.strip()
        if key == "date":
            return moment_format(when, fmt.strip() or "YYYY-MM-DD")
        if key == "time":
            return moment_format(dt.datetime.now(), fmt.strip() or "HH:mm")
        return _lookup(ctx, key)

    return OBSIDIAN.sub(obsidian, TEMPLATER.sub(templater,
                                                TEMPLATER_EXEC.sub("", raw)))


def heading_for(kind: str, body: str) -> str:
    """The section of `body` that a `kind` of entry belongs in.

    Returns the first candidate the note actually has, so V writes into the
    log that already exists rather than starting a second one beside it. Falls
    back to the canonical name, which is what `Vault.append` will create.
    """
    candidates = HEADINGS.get(kind, (kind,))
    present = {m.group(2).strip().lower() for m in notes.HEADING.finditer(body)}
    for name in candidates:
        if name.lower() in present:
            return name
    return candidates[0]


def insert_under(body: str, kind: str, text: str) -> str:
    """Drop `text` into the right section of a rendered-but-unwritten body."""
    from .vaultfs import append_under_heading
    return append_under_heading(body, heading_for(kind, body), text)


def path_for(kind: str, settings: Settings | None = None):
    s = settings or default_settings
    name = TEMPLATES.get(kind)
    if not name:
        return None
    return s.vault / s.folders.templates / name


def render(kind: str, *, title: str = "", when: dt.date | None = None,
           context: dict | None = None,
           settings: Settings | None = None) -> notes.Note | None:
    """The filled template as a parsed note, or None if there isn't one."""
    path = path_for(kind, settings)
    if not path or not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return notes.parse_text(render_text(raw, title=title, when=when,
                                            context=context))
    except Exception:
        # A template someone is mid-edit must not block a capture.
        return None


def fill(kind: str, meta: dict | None = None, *, title: str = "",
         when: dt.date | None = None, context: dict | None = None,
         settings: Settings | None = None) -> tuple[dict, str] | None:
    """`(meta, body)` ready for `Vault.create`, with `meta` layered on top.

    The template supplies the shape — every field the schema wants, in the
    order the vault writes them — and the caller supplies the values it knows.
    Keys the caller passes as None still land, because a `sprint:` line with
    nothing after it is what the template would have written anyway and its
    absence is what makes a note look hand-made.
    """
    note = render(kind, title=title, when=when, context=context,
                  settings=settings)
    if note is None:
        return None
    merged = dict(note.meta)
    merged.update(meta or {})
    return merged, note.body
