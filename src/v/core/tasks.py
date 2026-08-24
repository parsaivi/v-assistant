"""Checkbox tasks, as a first-class queryable layer.

The vault has ~1,100 `- [ ]` lines across daily notes, 1on1 action items,
project notes and assignment notes. Before this module the assistant could not
see a single one of them, which is why "what are my priorities", "how far
behind am I", and "mark X done" all failed: every one of those is a task
question, and there was no task.

Design notes:

**Markdown is the storage.** There is no tasks table that the vault syncs
*from* — the checkbox line in the note is the truth, and this module is a
parser plus an index over it. That means a task edited in Obsidian by hand is
immediately correct here after a re-index, and there is no second copy to drift.

**A task inherits its note's date.** `- [ ] buy milk` in `2026-07-12.md` is a
task from July 12th, whether or not anyone wrote a due date on it. Without that
inheritance "overdue" would be empty for a vault that never uses `📅` markers —
which is this vault.

**Completion is a one-line rewrite, and it is checked.** `complete()` reads the
file, changes exactly one character range, and refuses to write if any other
line moved. Ticking a box is the one structural edit worth making automatic;
letting it silently reformat a hand-written note is not.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from . import notes
from .config import Settings, settings as default_settings

# `- [ ]`, `* [x]`, `+ [/]`, with any indent. The state character is kept raw
# so the writer can restore whatever style the note already used.
TASK_RE = re.compile(r"^(\s*)([-*+])\s+\[([^\]])\]\s*(.*)$")

OPEN, DONE, DOING, DROPPED = "open", "done", "doing", "dropped"

_STATE = {
    " ": OPEN, "": OPEN,
    "x": DONE, "X": DONE,
    "/": DOING, ">": DOING,
    "-": DROPPED, "~": DROPPED,
}

# Obsidian Tasks plugin emoji, plus the plain-text forms people actually type.
_DUE_RE = re.compile(
    r"(?:📅|⏳|🗓|due\s*:?|deadline\s*:?|by)\s*(\d{4}-\d{2}-\d{2})", re.I)
_DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
_BARE_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_HIGH_RE = re.compile(r"(⏫|🔺|❗|!!!|#p1|\burgent\b|\basap\b)", re.I)
_LOW_RE = re.compile(r"(🔽|⏬|#p3|\bsomeday\b|\bmaybe\b)", re.I)
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z؀-ۿ][\w؀-ۿ/-]*)")

# Text left over once the metadata markers are stripped — what a human would
# call the task.
_STRIP_RE = re.compile(
    r"(📅|⏳|🗓|✅|➕|🔁|⏫|🔺|🔽|⏬|❗)\s*\d{0,4}-?\d{0,2}-?\d{0,2}")

DAILY_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Headings under which a checkbox is a commitment rather than a bullet style.
# `02 - Work/Lessons/` writes its rules as `- [ ]` ("UUID PKs by default"), and
# MR notes carry a reviewer checklist — neither is something the user owes
# anyone. Without this distinction "what are my priorities" answers with 376
# items, which is the same as answering with none.
TASKISH_HEADING_RE = re.compile(
    r"\b(tasks?|to\s?-?do|action items?|next steps?|follow[\s-]?ups?|"
    r"backlog|checklist|plan|agenda|goals?|objectives?|"
    r"کارها|اقدامات|وظایف|برنامه)\b", re.I)

# Note types whose checkboxes are commitments regardless of heading.
COMMITMENT_TYPES = {"assignment", "issue", "sprint", "1on1", "goal"}

# ...and types that are records of what was learned or reviewed. A lesson's
# "- [ ] UUID PKs by default" is a rule, not something to do on Tuesday.
REFERENCE_TYPES = {"lesson", "brag", "wiki", "merge-request", "place",
                   "course", "person"}

# Folders whose checkboxes are reference material by default.
REFERENCE_FOLDERS = ("02 - Work/Lessons", "02 - Work/Wikis",
                     "02 - Work/Merge Requests", "05 - Templates",
                     "09 - Anti-Library", "00 - Home", "06 - Archive")

TASK, RECURRING, REFERENCE = "task", "recurring", "reference"

STRUCK_RE = re.compile(r"^~~.*~~$")


@dataclass
class Task:
    id: int
    rel: str
    line: int              # 0-based index into the file's lines
    state: str
    text: str              # cleaned, human-readable
    raw: str               # the full original line
    section: str = ""      # nearest enclosing heading
    area: str = ""         # Work / University / Life, from the section chain
    due: dt.date | None = None
    done_on: dt.date | None = None
    note_date: dt.date | None = None
    priority: str = ""     # high | low | ""
    tags: list[str] = field(default_factory=list)
    kind: str = "task"     # task (a commitment) | reference (a checklist item)
    recurring: bool = False  # the same checkbox appears across many notes
    carried: int = 1       # how many notes hold this same task (set by collapse)
    first_seen: dt.date | None = None

    @property
    def is_open(self) -> bool:
        return self.state in (OPEN, DOING)

    @property
    def norm_key(self) -> str:
        return normalise_text(self.text) or self.text.lower()

    @property
    def effective_date(self) -> dt.date | None:
        """The date this task is answerable against.

        An explicit due date wins; otherwise the note's own date. A task
        written in a daily note is about that day unless it says otherwise.
        """
        return self.due or self.note_date

    def age_days(self, today: dt.date | None = None) -> int | None:
        d = self.effective_date
        if not d:
            return None
        return ((today or dt.date.today()) - d).days

    def is_overdue(self, today: dt.date | None = None) -> bool:
        if not self.is_open:
            return False
        age = self.age_days(today)
        return age is not None and age > 0

    def link(self) -> str:
        stem = self.rel[:-3] if self.rel.endswith(".md") else self.rel
        return f"[[{stem}]]"

    def render(self, *, with_source: bool = True,
               today: dt.date | None = None) -> str:
        mark = {OPEN: "☐", DONE: "☑", DOING: "◐", DROPPED: "⊘"}[self.state]
        bits = [f"{mark} {self.text}"]
        if with_source:
            where = self.section or self.rel.rsplit("/", 1)[-1][:-3]
            bits.append(f"· {where}")
        if self.due:
            bits.append(f"· due {self.due.isoformat()}")
        elif self.note_date:
            age = self.age_days(today)
            if age and age > 0:
                bits.append(f"· {age}d old")
        if self.carried > 1:
            bits.append(f"· carried {self.carried}×")
        return " ".join(bits)

    def urgency(self, today: dt.date | None = None) -> float:
        """A rank, not a truth. Used to order "what should I do next".

        Deliberately simple and inspectable: an explicit deadline outranks a
        stale daily-note line, and something rewritten five days running
        outranks something written once — a task you keep copying forward is
        either the most important thing you have or the one you're avoiding,
        and both deserve to be at the top of the list.
        """
        today = today or dt.date.today()
        score = 0.0
        if self.priority == "high":
            score += 4.0
        elif self.priority == "low":
            score -= 2.0
        age = self.age_days(today)
        if self.due:
            days = (self.due - today).days
            if days < 0:
                score += 5.0 + min(-days, 30) * 0.15   # overdue deadline
            elif days == 0:
                score += 4.5
            elif days <= 2:
                score += 3.0
            elif days <= 7:
                score += 1.5
        elif age is not None:
            # Undated age is not monotonic urgency. A line written in a daily
            # note this week is live work; the same line from four months ago
            # is almost certainly abandoned, and ranking it first — which the
            # first version of this did — buries everything the user is
            # actually doing under April. So: recent is urgent, middle-aged is
            # nagging, ancient is rot, and rot is reported separately by
            # `backlog_shape` rather than presented as the next thing to do.
            if age <= 0:
                score += 3.5
            elif age <= 2:
                score += 3.0
            elif age <= 7:
                score += 2.2
            elif age <= 21:
                score += 1.6
            elif age <= 60:
                score += 0.8
            else:
                score += max(0.0, 0.8 - (age - 60) / 120.0)
        score += min(self.carried - 1, 5) * 0.6
        if self.state == DOING:
            score += 1.5                               # already started
        return score

    @property
    def is_rotting(self) -> bool:
        """Open, undated, and older than two months — a backlog problem rather
        than a to-do. Surfaced as a count, not as a recommendation."""
        return self.is_open and not self.due and (self.age_days() or 0) > 60


# Task ids must survive a JSON round-trip through the MCP layer. JSON numbers
# are IEEE doubles, so anything above 2^53 loses precision — and the agent,
# handed `588988339574306832`, correctly refused to tick a box it could no
# longer identify. 6 bytes keeps ids under 2^48: for a vault of a few thousand
# checkboxes the collision odds are around one in a billion, and unlike a
# rounded id a collision would at least be deterministic and reproducible.
ID_BYTES = 6


def task_id(rel: str, line: int) -> int:
    """Deterministic, for the same reason chunk ids are: a re-index must not
    renumber tasks, or a stored reference points at a different checkbox."""
    h = hashlib.blake2b(f"{rel}|{line}".encode(), digest_size=ID_BYTES).digest()
    return int.from_bytes(h, "big")


def _clean(text: str) -> str:
    out = _STRIP_RE.sub("", text)
    out = _DUE_RE.sub("", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)        # bold
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", out)  # italic
    out = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", out)  # md links
    out = re.sub(r"`([^`]+)`", r"\1", out)            # inline code
    out = re.sub(r"~~(.+?)~~", r"\1", out)            # strikethrough
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ·-—")


def normalise_text(text: str) -> str:
    """Text stripped to its identity, for spotting the same checkbox repeated
    across notes. Markdown, links and digits go; wording stays."""
    out = re.sub(r"\[\[[^\]]*\]\]", "", text.lower())
    out = re.sub(r"https?://\S+", "", out)
    out = re.sub(r"[^\w؀-ۿ ]+", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def note_date_of(note: notes.Note) -> dt.date | None:
    """A note's own date: frontmatter first, then a YYYY-MM-DD filename."""
    for key in ("date", "due-date", "created", "start"):
        d = notes.as_date(note.meta.get(key))
        if d:
            return d
    m = DAILY_DATE_RE.search(note.path.stem if note.path else note.rel)
    if m:
        return notes.as_date(m.group(1))
    return None


def _area_of(section_chain: list[str], rel: str) -> str:
    """Which part of life a task belongs to.

    Daily notes group tasks under `### Work` / `### University` / `### Life`,
    so the heading chain answers this directly. Everything else falls back to
    the top-level folder, which is the same information by another name.
    """
    for h in reversed(section_chain):
        low = h.lower().strip()
        for name in ("work", "university", "life", "gym", "personal"):
            if low == name or low.startswith(name + " "):
                return name.capitalize()
    if "/" not in rel:
        return ""          # a root-level note has no area; a filename isn't one
    top = rel.split("/", 1)[0]
    m = re.match(r"^\d\d\s*-\s*(.+)$", top)
    return m.group(1).strip() if m else top


def _kind_of(note: notes.Note, chain: list[str], due: dt.date | None,
             note_date: dt.date | None) -> str:
    """Commitment or reference?

    A checkbox is something the user owes when it sits under a heading that
    says so, in a note whose whole purpose is commitments, or when it carries
    its own date. Everything else is a bullet that happens to be square.
    """
    # A dated checkbox is a commitment wherever it lives — someone wrote a
    # deadline on it.
    if due:
        return TASK
    # Location beats heading. Lesson notes head their rules "## Checklist",
    # which the heading heuristic below reads as a to-do list; the folder says
    # plainly that these are things learned, not things owed.
    if (note.type or "").lower() in REFERENCE_TYPES:
        return REFERENCE
    if any(note.rel.startswith(f) for f in REFERENCE_FOLDERS):
        return REFERENCE
    if any(TASKISH_HEADING_RE.search(h) for h in chain):
        return TASK
    if (note.type or "").lower() in COMMITMENT_TYPES:
        return TASK
    if note.rel.startswith("01 - Daily Notes/") and note_date:
        return TASK
    return REFERENCE


def parse_note(note: notes.Note) -> list[Task]:
    """Every checkbox in one note."""
    out: list[Task] = []
    nd = note_date_of(note)
    chain: list[str] = []
    # Body offset: line numbers must index the *file*, not the body, or
    # completion rewrites the wrong line in any note with frontmatter.
    raw = note.path.read_text(encoding="utf-8", errors="replace") \
        if note.path and note.path.exists() else (
            notes.dump_frontmatter(note.meta) + "\n" + note.body
            if note.meta else note.body)
    in_code = False
    for i, line in enumerate(raw.splitlines()):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        h = notes.HEADING.match(line)
        if h:
            level = len(h.group(1))
            chain = chain[:level - 1] + [h.group(2).strip()]
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        _indent, _bullet, mark, body = m.groups()
        state = _STATE.get(mark, OPEN)
        # `- [ ] ~~repair the package: failed~~` is an abandoned task written
        # the way people actually abandon them — struck through, box left
        # unticked. Reading it as open resurrects work the user already gave up.
        if STRUCK_RE.match(body.strip()) and state == OPEN:
            state = DROPPED
        text = _clean(body)
        # An empty checkbox is a template placeholder, and so is a bare label
        # like `- [ ] KR1:` — the frame of a task the user never filled in.
        if not text or len(re.sub(r"[^\w؀-ۿ]", "", text)) < 3:
            continue
        if text.endswith(":") and len(text) <= 8:
            continue
        due = None
        dm = _DUE_RE.search(body)
        if dm:
            due = notes.as_date(dm.group(1))
        elif not _DONE_DATE_RE.search(body):
            bm = _BARE_DATE_RE.search(body)
            if bm:
                due = notes.as_date(bm.group(1))
        done_on = None
        dd = _DONE_DATE_RE.search(body)
        if dd:
            done_on = notes.as_date(dd.group(1))
        priority = ("high" if _HIGH_RE.search(body)
                    else "low" if _LOW_RE.search(body) else "")
        out.append(Task(
            id=task_id(note.rel, i), rel=note.rel, line=i, state=state,
            text=text, raw=line, section=chain[-1] if chain else "",
            area=_area_of(chain, note.rel), due=due, done_on=done_on,
            note_date=nd, priority=priority,
            tags=sorted({t.group(1) for t in _TAG_RE.finditer(body)}),
            kind=_kind_of(note, chain, due, nd),
        ))
    return out


# ---------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id        INTEGER PRIMARY KEY,
    rel       TEXT NOT NULL,
    line      INTEGER NOT NULL,
    state     TEXT NOT NULL,
    text      TEXT NOT NULL,
    raw       TEXT,
    section   TEXT,
    area      TEXT,
    due       TEXT,
    done_on   TEXT,
    note_date TEXT,
    priority  TEXT,
    tags      TEXT,
    norm      TEXT,
    kind      TEXT DEFAULT 'task',
    recurring INTEGER DEFAULT 0
);
"""

# Kept apart from the table DDL: `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing table, so an index over a column added later must be created *after*
# the ALTERs in `ensure_schema`, not in the same script.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_rel   ON tasks(rel);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_due   ON tasks(due);
CREATE INDEX IF NOT EXISTS idx_tasks_norm  ON tasks(norm);
"""

# How many distinct notes must carry the identical checkbox before it stops
# counting as a task. Tuned against this vault: the MR template's review
# checklist ("Add Unit tests for the changes") repeats across ~180 MR notes,
# and `- [ ] Trained today` sits in every daily note. Both are rituals or
# boilerplate, and both were burying the 3 real tasks under 800.
RECURRING_MIN_NOTES = 4


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Older databases predate these columns; adding them here keeps a rebuild
    # from being mandatory after an upgrade.
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, decl in (("norm", "TEXT"), ("kind", "TEXT DEFAULT 'task'"),
                      ("recurring", "INTEGER DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    conn.executescript(INDEXES)


def classify_recurring(conn: sqlite3.Connection,
                       min_notes: int = RECURRING_MIN_NOTES) -> int:
    """Flag checkboxes that appear verbatim across many notes.

    Derived from the data rather than a hardcoded blocklist: whatever template
    the user adopts next gets handled without a code change, and a genuinely
    repeated intention ("Trained today") is still visible — it is flagged, not
    deleted.

    This is a separate column from `kind` deliberately. `kind` is what the
    parser saw in one note; `recurring` is a whole-vault judgement recomputed
    on every rebuild. Folding them into one column meant the recompute had to
    reset `kind` first, which threw away the parser's commitment/reference call.
    """
    conn.execute("UPDATE tasks SET recurring=0")
    conn.execute(
        "UPDATE tasks SET recurring=1 WHERE norm IN ("
        "  SELECT norm FROM tasks WHERE norm <> ''"
        "  GROUP BY norm HAVING COUNT(DISTINCT rel) >= ?)", (min_notes,))
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE recurring=1").fetchone()[0]
    conn.commit()
    return n


def replace_for_note(conn: sqlite3.Connection, rel: str,
                     tasks: Iterable[Task]) -> int:
    conn.execute("DELETE FROM tasks WHERE rel=?", (rel,))
    n = 0
    for t in tasks:
        conn.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id, rel, line, state, text, raw, section, area, due, done_on,"
            "  note_date, priority, tags, norm, kind, recurring)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.id, t.rel, t.line, t.state, t.text, t.raw, t.section, t.area,
             t.due.isoformat() if t.due else None,
             t.done_on.isoformat() if t.done_on else None,
             t.note_date.isoformat() if t.note_date else None,
             t.priority, ",".join(t.tags), normalise_text(t.text), t.kind,
             int(t.recurring)))
        n += 1
    return n


def delete_for_note(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM tasks WHERE rel=?", (rel,))


def _from_row(r: sqlite3.Row) -> Task:
    return Task(
        id=r["id"], rel=r["rel"], line=r["line"], state=r["state"],
        text=r["text"], raw=r["raw"] or "", section=r["section"] or "",
        area=r["area"] or "",
        due=notes.as_date(r["due"]), done_on=notes.as_date(r["done_on"]),
        note_date=notes.as_date(r["note_date"]), priority=r["priority"] or "",
        tags=[t for t in (r["tags"] or "").split(",") if t],
        kind=(r["kind"] if "kind" in r.keys() else None) or TASK,
        recurring=bool(r["recurring"] if "recurring" in r.keys() else 0),
    )


# ---------------------------------------------------------------------
def _tokens(s: str) -> set[str]:
    return {w for w in re.split(r"[^\w؀-ۿ]+", s.lower()) if len(w) > 1}


def match_score(query: str, t: Task) -> float:
    """How well a free-text phrase names this task.

    Substring beats overlap: "mark the debugging task done" should find
    `- [ ] debugging the worker` even though only one word is shared, and
    should not be outranked by a task that happens to share "the" and "task".
    """
    q, text = query.lower().strip(), t.text.lower()
    if not q:
        return 0.0
    if q == text:
        return 1.0
    score = 0.0
    if q in text:
        score = 0.85 + 0.1 * (len(q) / max(len(text), 1))
    elif text in q:
        score = 0.8
    qt, tt = _tokens(q), _tokens(text)
    if qt and tt:
        overlap = len(qt & tt) / len(qt | tt)
        # A single distinctive word matching is worth more than the Jaccard
        # ratio suggests when the query is one word ("debugging").
        best_word = max((len(w) for w in qt & tt), default=0)
        score = max(score, overlap + (0.25 if best_word >= 5 else 0.0))
    return min(score, 1.0)


def _count_by(rows: list[Task], attr: str) -> dict:
    out: dict[str, int] = {}
    for t in rows:
        k = getattr(t, attr) or "—"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def collapse(rows: list[Task]) -> list[Task]:
    """One entry per distinct task, however many notes it was copied into.

    Carrying an unfinished task forward by pasting it into tomorrow's daily
    note is the normal way people use this vault. Listing all three copies
    turns one nagging task into three, and makes the backlog look worse than
    it is. The surviving copy points at the most recent note — that's the one
    to tick — while `carried` records how long it's been getting rewritten.
    """
    best: dict[str, Task] = {}
    counts: dict[str, int] = {}
    first: dict[str, dt.date] = {}
    for t in rows:
        key = t.norm_key
        counts[key] = counts.get(key, 0) + 1
        d = t.effective_date
        if d and (key not in first or d < first[key]):
            first[key] = d
        prev = best.get(key)
        if prev is None or (t.effective_date or dt.date.min) > (
                prev.effective_date or dt.date.min):
            best[key] = t
    out = []
    for key, t in best.items():
        if counts[key] > 1:
            t.carried = counts[key]
            t.first_seen = first.get(key)
        out.append(t)
    return out


class TaskStore:
    def __init__(self, conn: sqlite3.Connection,
                 settings: Settings | None = None):
        self.conn = conn
        self.s = settings or default_settings
        ensure_schema(self.conn)

    # ---- reading ----------------------------------------------------
    def _select(self, where: str = "", params: tuple = (),
                order: str = "") -> list[Task]:
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + where
        sql += order or " ORDER BY COALESCE(due, note_date) DESC, rel, line"
        return [_from_row(r) for r in self.conn.execute(sql, params)]

    def all(self) -> list[Task]:
        return self._select()

    def open(self, *, area: str | None = None, folder: str | None = None,
             include_doing: bool = True,
             include_recurring: bool = False) -> list[Task]:
        states = (OPEN, DOING) if include_doing else (OPEN,)
        where = f"state IN ({','.join('?' * len(states))})"
        params: list = list(states)
        if not include_recurring:
            where += " AND kind=? AND recurring=0"
            params.append(TASK)
        if area:
            where += " AND LOWER(area)=?"
            params.append(area.lower())
        if folder:
            where += " AND rel LIKE ?"
            params.append(folder.rstrip("/") + "/%")
        # Templates are placeholders; a template checkbox is not a task.
        where += " AND rel NOT LIKE ?"
        params.append(self.s.folders.templates + "/%")
        # Archived notes are closed by definition — a checkbox left unticked in
        # `06 - Archive/` is history, not a commitment.
        where += " AND rel NOT LIKE ?"
        params.append(self.s.folders.archive.rstrip("/") + "/%")
        return self._select(where, tuple(params))

    def rituals(self) -> list[Task]:
        """The repeated checkboxes, kept queryable but out of the way —
        `- [ ] Trained today` still matters on the day it's asked about."""
        return self._select("recurring=1 AND state IN ('open','doing')")

    def reference(self) -> list[Task]:
        """Checklist items in lesson/wiki/MR notes — square bullets, not debts."""
        return self._select("kind=? AND state IN ('open','doing')", (REFERENCE,))

    def done(self, since: dt.date | None = None) -> list[Task]:
        rows = self._select("state=?", (DONE,))
        if since:
            rows = [t for t in rows
                    if (t.done_on or t.note_date or dt.date.min) >= since]
        return rows

    def overdue(self, today: dt.date | None = None,
                dedupe: bool = True) -> list[Task]:
        today = today or dt.date.today()
        rows = [t for t in self.open() if t.is_overdue(today)]
        if dedupe:
            rows = collapse(rows)
        return sorted(rows, key=lambda t: (t.effective_date or dt.date.max))

    def priorities(self, *, today: dt.date | None = None, limit: int = 10,
                   area: str | None = None) -> list[Task]:
        """The open commitments most worth doing next, ranked."""
        today = today or dt.date.today()
        rows = collapse(self.open(area=area))
        rows.sort(key=lambda t: (-t.urgency(today),
                                 t.effective_date or dt.date.max))
        # Diversity. A planning note written yesterday contains twenty tasks
        # that all score identically, and without this the answer to "what are
        # my priorities" is that one note's contents — technically the top ten,
        # practically a list that ignores the rest of the user's life.
        seen: dict[str, int] = {}
        out: list[Task] = []
        for t in rows:
            n = seen.get(t.rel, 0)
            if n >= 3:
                continue
            seen[t.rel] = n + 1
            out.append(t)
            if len(out) >= limit:
                break
        return out

    def backlog_shape(self, today: dt.date | None = None) -> dict:
        """The numbers behind "how far behind am I" — counted, not guessed."""
        today = today or dt.date.today()
        rows = collapse(self.open())
        overdue = [t for t in rows if t.is_overdue(today)]
        ages = sorted((t.age_days(today) or 0) for t in overdue)
        week_ago = today - dt.timedelta(days=7)
        return {
            "open": len(rows),
            "overdue": len(overdue),
            "oldest_days": ages[-1] if ages else 0,
            "median_days": ages[len(ages) // 2] if ages else 0,
            "due_today": len([t for t in rows if t.effective_date == today]),
            "carried": len([t for t in rows if t.carried > 2]),
            "rotting": len([t for t in rows if t.is_rotting]),
            "live": len([t for t in rows
                         if (t.age_days(today) or 0) <= 21]),
            "closed_last_7": len([t for t in self.done()
                                  if (t.done_on or t.note_date or dt.date.min)
                                  >= week_ago]),
            "by_area": _count_by(rows, "area"),
        }

    def due_on(self, day: dt.date) -> list[Task]:
        return [t for t in self.open() if t.effective_date == day]

    def due_by(self, day: dt.date) -> list[Task]:
        return sorted((t for t in self.open()
                       if t.effective_date and t.effective_date <= day),
                      key=lambda t: (t.effective_date or dt.date.max))

    def find(self, text: str, *, limit: int = 5,
             threshold: float = 0.45,
             open_only: bool = True) -> list[tuple[Task, float]]:
        pool = self.open() if open_only else self.all()
        scored = [(t, match_score(text, t)) for t in pool]
        scored = [(t, sc) for t, sc in scored if sc >= threshold]
        scored.sort(key=lambda p: (-p[1], p[0].rel))
        return scored[:limit]

    def get(self, task_id: int) -> Task | None:
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (task_id,)).fetchone()
        return _from_row(r) if r else None

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) c FROM tasks GROUP BY state").fetchall()
        return {r["state"]: r["c"] for r in rows}

    # ---- writing ----------------------------------------------------
    def complete(self, task: Task, vault, *, state: str = DONE,
                 today: dt.date | None = None) -> str:
        """Tick the box, in place, changing exactly one line.

        Goes through `vault.rewrite(allow_rewrite=True)` so it is still a
        `[bot]` commit and still undoable — but verifies first that nothing
        except the target line differs, because a rewrite that reformats a
        hand-written note is exactly the failure the append-only rule exists
        to prevent.
        """
        path = vault.path(task.rel)
        lines = path.read_text(encoding="utf-8").splitlines()
        if task.line >= len(lines):
            raise ValueError(
                f"{task.rel} has changed since it was indexed — re-index first")
        current = lines[task.line]
        m = TASK_RE.match(current)
        if not m:
            raise ValueError(
                f"line {task.line + 1} of {task.rel} is no longer a checkbox "
                f"({current.strip()[:40]!r}) — re-index first")

        mark = {DONE: "x", OPEN: " ", DOING: "/", DROPPED: "-"}[state]
        indent, bullet, _old, body = m.groups()
        if state == DONE and not _DONE_DATE_RE.search(body):
            body = body.rstrip() + f" ✅ {(today or dt.date.today()).isoformat()}"
        new_line = f"{indent}{bullet} [{mark}] {body}"

        updated = list(lines)
        updated[task.line] = new_line
        differing = [i for i, (a, b) in enumerate(zip(lines, updated)) if a != b]
        if differing != [task.line] or len(updated) != len(lines):
            raise ValueError("refusing to write: more than the task line changed")

        text = "\n".join(updated) + "\n"
        verb = {DONE: "complete", OPEN: "reopen", DOING: "start",
                DROPPED: "drop"}[state]
        vault.rewrite(task.rel, text, allow_rewrite=True,
                      summary=f"{verb} task: {task.text[:60]}")
        # Keep the index honest without a full re-scan of the vault.
        self.conn.execute(
            "UPDATE tasks SET state=?, raw=?, done_on=? WHERE id=?",
            (state, new_line,
             (today or dt.date.today()).isoformat() if state == DONE else None,
             task.id))
        self.conn.commit()
        # The row is right, but `notes.mtime` is now behind the file, so
        # `is_stale()` would order a full rebuild on the next startup for a
        # change already applied.
        self._resync(task.rel)
        return new_line.strip()

    def add(self, text: str, vault, *, rel: str, heading: str | None = None,
            due: dt.date | None = None) -> str:
        """Append a new checkbox. Ordinary append — no rewrite involved."""
        line = f"- [ ] {text}"
        if due:
            line += f" 📅 {due.isoformat()}"
        vault.append(rel, line, heading=heading,
                     summary=f"add task: {text[:60]}")
        # Markdown is the storage, but the *mirror* is what every task query
        # reads. Without this the task was on disk and invisible — added, then
        # missing from the priorities list it was added to.
        self._resync(rel)
        return line

    def _resync(self, rel: str) -> None:
        """Re-index the one note that changed. Imported here rather than at
        module scope: `index` imports this module."""
        from . import index
        try:
            index.resync_note(self.conn, rel, self.s)
        except (OSError, UnicodeDecodeError, sqlite3.Error):
            # The write succeeded; a stale mirror is recoverable on the next
            # index run and is not worth failing the write over.
            pass
