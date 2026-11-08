"""Who wrote each block of the vault, and whether Parsa has read what V wrote.

**The failure this exists for.** Parsa asked V to summarise his plans, break
goals into steps, draft a week. Every answer landed in the vault, and most were
never read. A month later V searched the vault, found those notes, and treated
them as what Parsa had decided — they were indistinguishable from his own
writing. One model mistake, written down once, could steer every later answer
and, through those answers, his own thinking.

The idea is ClawMMU's (Hu et al., arXiv:2610.28093): every piece of an agent's
memory carries a label saying where it came from; anything a model wrote starts
at the lowest trust level; only the owner raises it — authority never lifts
itself. Two labels are enough here, because the question that matters is
binary: **did a model write this text?**

* ``parsa`` — no model was involved. His own words (a capture of his message),
  or rules applied to what he said and to templates he wrote: a task line, a
  schedule block, a scaffolded daily note. Text with no record at all is also
  his, because he writes in Obsidian directly and Obsidian tells V nothing.
* ``v`` — a model composed, summarised, rewrote, classified or generated it.

**Fail closed.** A write that does not say who wrote it is ``v``, never
``parsa``. The mistake that costs something is V's text passing as Parsa's; the
reverse costs one approval.

**Why a side store, not markers in the notes.** Half the vault is parsed line
by line — schedule blocks, checkboxes, ledger rows with ``[field:: value]``,
the bare-bullet Likes list, the frontmatter Dataview reads — and a marker
appended to any of those lines breaks its parser silently. So labels live in
SQLite under the state directory, keyed by note path and a hash of each block
written. A block is found again *by its text*: if it is still in the note,
the label applies; if Parsa rewrote it by hand, the text no longer matches and
it is his. That is the rule "if Parsa edits V's text himself, it becomes his",
and it costs no bookkeeping.

**An approval binds to the hash** of the exact block Parsa approved. If V later
changes that text, the new text is a new block with no approval. Approvals are
append-only — a trigger refuses UPDATE and DELETE — and only two code paths
write them: ``v approve`` at a terminal and the owner's Telegram button, both
through ``Engine.act`` or ``cli.py``. No MCP tool and no route a model can
reach calls ``approve``; ``tests/test_provenance.py`` asserts it by reading the
source, which is the only check that cannot be fooled by a tool someone adds
next month.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import difflib
import hashlib
import os
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

PARSA = "parsa"
V = "v"
SOURCES = (PARSA, V)

# Line labels. `None` is Parsa's.
UNAPPROVED = "v"
APPROVED = "v-approved"
PARTLY = "v-partly"          # a fragment that mixes his text and V's

OPEN = {
    UNAPPROVED: "[written by V, not approved]",
    APPROVED: "[written by V, approved by Parsa]",
    PARTLY: "[partly written by V, not approved]",
}
CLOSE = "[/written by V]"

# Set on every process V's model shell launches (see `shells.ClaudeCodeShell`),
# and inherited by everything it starts — the MCP server and any Bash command
# included. Approval writers refuse to run under it. Not a wall against a shell
# determined to `unset` it; a second fence behind the real one, which is that
# nothing the model is granted calls an approval writer at all.
MODEL_SHELL_ENV = "V_MODEL_SHELL"

# `[bot]` commit subjects written by code with no model in the loop: the
# template scaffolds, the generated sections, the docs built from source.
# Used only by `backfill`, which has nothing but the commit subject to go on.
# Every other `[bot]` subject is `v` — the list is an allowlist on purpose, so
# a subject nobody thought about lands on the safe side.
DETERMINISTIC = (
    "scaffold daily note", "scaffold weekly note",
    "sync calendar into", "sync schedule into",
    "add the Schedule section to the daily template",
    "V skills", "V goals", "V readme",
)

# Shortest line a *partial* match may claim. A vanished block's lines are
# still V's wherever they survive verbatim — but "---", "## Tasks" and "- [ ]"
# survive everywhere, and claiming those would mark Parsa's own skeleton.
_MIN_PARTIAL = 12

_author: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "v_author", default=None)


class Refused(RuntimeError):
    """An approval attempted from somewhere approvals cannot come from."""


@contextlib.contextmanager
def authored_by(source: str):
    """Every vault write inside this block is labelled `source`, unless the
    write itself says otherwise.

    For call trees where the decision belongs at the top — the engine knows a
    capture is Parsa's words; `tasks.add` three calls down does not, and is
    also called by the MCP server on a model's behalf. A context variable
    rather than a parameter threaded through every helper, because the
    helpers are shared and the parameter would have to be right in all of
    them; this has to be right in one place.
    """
    token = _author.set(source)
    try:
        yield
    finally:
        _author.reset(token)


def resolve(source: str | None = None, *, floor: str | None = None) -> str:
    """The label a write gets. Explicit beats context beats the default, and
    the default is `v`. A `floor` of `v` wins over everything: the MCP
    server's writer is built that way, so no tool can claim to be Parsa."""
    if floor == V or os.environ.get(MODEL_SHELL_ENV):
        # Anything written from a process a model shell started — the MCP
        # server, or a `v week --add` the model ran through Bash — is the
        # model's, whatever code path it took to get here.
        return V
    for s in (source, _author.get(), floor):
        if s is not None:
            return s if s in SOURCES else V
    return V


def refuse_in_model_shell() -> None:
    if os.environ.get(MODEL_SHELL_ENV):
        raise Refused(
            "approvals come from Parsa — the Telegram button or `v approve` at "
            "his own terminal — never from inside a model's shell")


# ---- text -------------------------------------------------------------------
def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]


def _trim(lines: list[str]) -> list[str]:
    i, j = 0, len(lines)
    while i < j and not lines[i].strip():
        i += 1
    while j > i and not lines[j - 1].strip():
        j -= 1
    return lines[i:j]


def block_hash(text: str) -> str:
    """sha256 of a block's lines, trailing whitespace and the blank lines at
    either end dropped — the parts no reader would call a change."""
    return hashlib.sha256(
        "\n".join(_trim(_lines(text))).encode("utf-8")).hexdigest()


def added_blocks(before: str, after: str) -> list[str]:
    """The runs of lines `after` has that `before` did not.

    A diff rather than "whatever the caller passed", so one rule covers every
    write path: an append adds a run at the end, an edit replaces a run in the
    middle, a whole-note rewrite that changed one section adds only that
    section. The lines a rewrite left alone keep whatever label they had.
    """
    a = _lines(before) if before else []
    b = _lines(after)
    out = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            run = _trim(b[j1:j2])
            if run:
                out.append("\n".join(run))
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Pending:
    """One run of V's text, in a note, that Parsa has not approved."""

    rel: str
    line: int        # 0-based, into the whole file
    text: str

    def preview(self, width: int = 140) -> str:
        t = _squash(self.text)
        return t[:width] + ("…" if len(t) > width else "")


# ---- the store ----------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    id        INTEGER PRIMARY KEY,
    rel       TEXT NOT NULL,
    hash      TEXT NOT NULL,
    source    TEXT NOT NULL,
    text      TEXT NOT NULL,
    commit_id TEXT,
    at        TEXT NOT NULL,
    origin    TEXT NOT NULL DEFAULT 'write',
    UNIQUE (rel, hash)
);
CREATE INDEX IF NOT EXISTS blocks_rel ON blocks (rel, source);
CREATE INDEX IF NOT EXISTS blocks_at ON blocks (at);
CREATE TABLE IF NOT EXISTS approvals (
    id   INTEGER PRIMARY KEY,
    rel  TEXT NOT NULL,
    hash TEXT NOT NULL,
    by   TEXT NOT NULL,
    via  TEXT NOT NULL,
    at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_rel ON approvals (rel, hash);
CREATE TRIGGER IF NOT EXISTS approvals_append_only_u BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_append_only_d BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()


def _now() -> str:
    # Microseconds, because "what did this turn write" is a range query on
    # this column, and two turns a second apart must not overlap.
    return dt.datetime.now().isoformat(timespec="microseconds")


class Store:
    """Labels and approvals for one vault, in `state_dir/provenance.db`.

    One connection per thread and path — the MCP server answers tool calls on
    worker threads, and an sqlite3 connection belongs to the thread that
    opened it. WAL, because the Telegram service, the scheduler and the MCP
    server are three processes writing the same file.
    """

    def __init__(self, settings):
        self.s = settings
        self.path: Path = settings.state_dir / "provenance.db"
        self.root: Path = settings.vault

    # ---- plumbing -------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        cache = getattr(_local, "conns", None)
        if cache is None:
            cache = _local.conns = {}
        key = str(self.path)
        c = cache.get(key)
        if c is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, timeout=10)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            cache[key] = c
        return c

    def _read(self, rel: str) -> str | None:
        p = self.root / rel
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return None

    # ---- writing labels (vaultfs calls these, and nothing else should) --
    def stage(self, rel: str, before: str, after: str, source: str, *,
              origin: str = "write") -> list[int]:
        """Record the blocks a write is about to add. Called *before* the file
        is written, so a store that cannot be written stops the write — a V
        block that reached the disk unlabelled would read as Parsa's.

        On a hash already recorded, `v` wins: identical text written once by
        Parsa and once by V cannot be told apart, so it is treated as V's.
        """
        blocks = added_blocks(before, after)
        return self._insert(rel, blocks, source, origin=origin)

    def stage_binary(self, rel: str, data: bytes, source: str) -> list[int]:
        h = hashlib.sha256(data).hexdigest()
        return self._insert_hashed(rel, [(h, f"(binary, {len(data)} bytes)")],
                                   source, origin="write")

    def _insert(self, rel, blocks, source, *, origin, commit=None, at=None):
        rows = [(block_hash(b), "\n".join(_trim(_lines(b)))) for b in blocks]
        return self._insert_hashed(rel, rows, source, origin=origin,
                                   commit=commit, at=at)

    def _insert_hashed(self, rel, rows, source, *, origin, commit=None,
                       at=None, keep_existing: bool = False) -> list[int]:
        source = source if source in SOURCES else V
        ids = []
        c = self.conn
        with c:
            for h, text in rows:
                if keep_existing:
                    c.execute(
                        "INSERT OR IGNORE INTO blocks (rel, hash, source, text,"
                        " commit_id, at, origin) VALUES (?,?,?,?,?,?,?)",
                        (rel, h, source, text, commit, at or _now(), origin))
                else:
                    c.execute(
                        "INSERT INTO blocks (rel, hash, source, text, commit_id,"
                        " at, origin) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT (rel, hash) DO UPDATE SET "
                        "source = CASE WHEN excluded.source = 'v' THEN 'v' "
                        "ELSE blocks.source END, at = excluded.at, "
                        "commit_id = excluded.commit_id, origin = excluded.origin",
                        (rel, h, source, text, commit, at or _now(), origin))
                row = c.execute("SELECT id FROM blocks WHERE rel=? AND hash=?",
                                (rel, h)).fetchone()
                ids.append(row[0])
        return ids

    def stamp(self, ids: list[int], commit: str | None) -> None:
        """Attach the commit once git has one. Best-effort: the label is what
        matters, and it is already on disk."""
        if not ids or not commit:
            return
        try:
            with self.conn as c:
                c.executemany("UPDATE blocks SET commit_id=? WHERE id=?",
                              [(commit, i) for i in ids])
        except sqlite3.Error:
            pass

    # ---- reading labels --------------------------------------------------
    def _v_blocks(self, rel: str) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT hash, text FROM blocks WHERE rel=? AND source='v' "
            "ORDER BY id", (rel,)).fetchall()

    def _approved(self, rel: str) -> set[str]:
        return {h for (h,) in self.conn.execute(
            "SELECT DISTINCT hash FROM approvals WHERE rel=?", (rel,))}

    def labels(self, rel: str, text: str | None = None) -> list[str | None]:
        """One label per line of the note: None (Parsa's), UNAPPROVED or
        APPROVED.

        Whole blocks first. A V block still present verbatim labels its lines,
        approved or not; where two overlap, unapproved wins. Then the remnants:
        a block no longer present as a whole (Parsa edited one line of it, or
        V edited part of it) still owns its surviving lines, but unapproved —
        the approval was for the whole block, and that block is gone.
        """
        if text is None:
            text = self._read(rel) or ""
        lines = _lines(text)
        n = len(lines)
        try:
            rows = self._v_blocks(rel)
        except sqlite3.Error:
            rows = []
        if not rows:
            return [None] * n
        approved = self._approved(rel)
        where: dict[str, list[int]] = {}
        for i, ln in enumerate(lines):
            if ln.strip():
                where.setdefault(ln, []).append(i)

        whole: list[str | None] = [None] * n
        gone: list[list[str]] = []
        for h, btext in rows:
            blines = _lines(btext)
            if not blines or not blines[0].strip():
                continue
            hit = False
            for start in where.get(blines[0], ()):
                if lines[start:start + len(blines)] == blines:
                    hit = True
                    lab = APPROVED if h in approved else UNAPPROVED
                    for k in range(start, start + len(blines)):
                        if whole[k] != UNAPPROVED:
                            whole[k] = lab
            if not hit:
                gone.append(blines)

        out = list(whole)
        for blines in gone:
            for ln in blines:
                s = ln.strip()
                if len(s) < _MIN_PARTIAL or s.startswith("#") or s == "---":
                    continue
                for i in where.get(ln, ()):
                    if out[i] is None:
                        out[i] = UNAPPROVED
        return out

    def line_label(self, rel: str, line: str) -> str | None:
        """The label of one line, found by its text in the note."""
        want = line.rstrip()
        text = self._read(rel)
        if text is None:
            return None
        found = None
        for ln, lab in zip(_lines(text), self.labels(rel, text)):
            if ln == want and lab:
                if lab == UNAPPROVED:
                    return UNAPPROVED
                found = lab
        return found

    def derived(self, sources: list[str], key: str = "") -> str:
        """The label for a copy rendered from other notes.

        The calendar and schedule syncs copy a day's lines into its daily
        note. Labelling the copy `parsa` because the sync is deterministic
        would launder V's text: an event the model added would come out the
        other side as Parsa's. So a copy is `v` if any unapproved V line it
        could have come from (a line in `sources` containing `key`, the day's
        ISO date) is still there.
        """
        for rel in sources:
            text = self._read(rel)
            if not text:
                continue
            for ln, lab in zip(_lines(text), self.labels(rel, text)):
                if lab == UNAPPROVED and key in ln:
                    return V
        return PARSA

    # ---- marking, for anything that hands note text to a model ----------
    def mark(self, rel: str, text: str,
             labels: list[str | None] | None = None) -> str:
        """The note with V's runs fenced and Parsa's left exactly as written."""
        labels = labels if labels is not None else self.labels(rel, text)
        lines = text.replace("\r\n", "\n").split("\n")
        return _wrap(lines, _smooth(lines, labels))

    def mark_note(self, rel: str, raw: str, body: str) -> tuple[str | None, str]:
        """(label of the frontmatter, marked body) for a note read whole.

        `notes.parse` makes the body a suffix of the raw file, so the body's
        labels are the tail of the file's.
        """
        labels = self.labels(rel, raw)
        nb = len(body.replace("\r\n", "\n").split("\n"))
        head, tail = labels[:len(labels) - nb], labels[len(labels) - nb:]
        meta = (UNAPPROVED if UNAPPROVED in head else
                APPROVED if APPROVED in head else None)
        return meta, self.mark(rel, body, tail)

    def mark_fragment(self, rel: str, fragment: str) -> str:
        """Mark a passage cut from a note — a search hit, a RAG chunk, a
        preview with its newlines squashed.

        Its lines may be truncated or joined, so they are matched to the
        note's lines by containment rather than equality. A line that
        contains both V's text and Parsa's cannot be split honestly, so it is
        fenced as *partly* V's.
        """
        text = self._read(rel)
        if not fragment or text is None:
            return fragment
        labels = self.labels(rel, text)
        if not any(labels):
            return fragment
        v_lines, p_lines = [], []
        for ln, lab in zip(_lines(text), labels):
            s = _squash(ln)
            if not s:
                continue
            (v_lines if lab else p_lines).append((s, lab))
        v_exact = {}
        for s, lab in v_lines:
            if v_exact.get(s) != UNAPPROVED:
                v_exact[s] = lab
        p_exact = {s for s, _ in p_lines}

        def label(line: str) -> str | None:
            s = _squash(line)
            if not s:
                return None
            if s in v_exact and s not in p_exact:
                return v_exact[s]
            if s in p_exact:
                return None
            hits = [lab for vs, lab in v_lines
                    if (len(vs) >= _MIN_PARTIAL and vs in s)
                    or (len(s) >= _MIN_PARTIAL and s.rstrip("…") in vs)]
            if not hits:
                return None
            mixed = any(len(ps) >= _MIN_PARTIAL and ps in s
                        for ps, _ in p_lines)
            if mixed:
                return PARTLY
            return UNAPPROVED if UNAPPROVED in hits else APPROVED

        flines = fragment.split("\n")
        return _wrap(flines, _smooth(flines, [label(ln) for ln in flines]))

    # ---- what is waiting for Parsa ---------------------------------------
    def pending(self, rel: str | None = None) -> list[Pending]:
        """Runs of V's text not yet approved, as they stand in the notes now."""
        if rel is not None:
            rels = [rel]
        else:
            rels = [r for (r,) in self.conn.execute(
                "SELECT DISTINCT rel FROM blocks WHERE source='v' ORDER BY rel")]
        out = []
        for r in rels:
            text = self._read(r)
            if text is None:
                continue
            lines = _lines(text)
            labels = self.labels(r, text)
            i = 0
            while i < len(lines):
                if labels[i] == UNAPPROVED:
                    j = i
                    while j < len(lines) and (
                            labels[j] == UNAPPROVED
                            or (not lines[j].strip() and j + 1 < len(lines)
                                and labels[j + 1] == UNAPPROVED)):
                        j += 1
                    run = "\n".join(_trim(lines[i:j]))
                    if run:
                        out.append(Pending(r, i, run))
                    i = j
                else:
                    i += 1
        return out

    def written_since(self, since: str) -> list[tuple[int, str]]:
        """(block id, note) for V's blocks recorded at or after `since` that
        are still in their note and still unapproved — what a turn just wrote,
        for the Approve button on its reply. Read from the store, not from the
        turn, because most of V's writes happen in the MCP server's process."""
        rows = self.conn.execute(
            "SELECT id, rel, hash, text FROM blocks WHERE source='v' AND at >= ?"
            " AND origin = 'write' ORDER BY id", (since,)).fetchall()
        out = []
        for bid, rel, h, btext in rows:
            if h in self._approved(rel):
                continue
            text = self._read(rel)
            if text is None or not _contains(_lines(text), _lines(btext)):
                continue
            out.append((bid, rel))
        return out

    # ---- approving: Parsa only -------------------------------------------
    def _approve_rows(self, rows: list[tuple[str, str]], *, by: str,
                      via: str) -> int:
        refuse_in_model_shell()
        at = _now()
        with self.conn as c:
            c.executemany(
                "INSERT INTO approvals (rel, hash, by, via, at) "
                "VALUES (?,?,?,?,?)", [(r, h, by, via, at) for r, h in rows])
        return len(rows)

    def approve_ids(self, ids: list[int], *, by: str, via: str) -> int:
        """Approve specific blocks — the ones a reply said it wrote. A block
        whose text has since changed has a different hash and is not among
        these, so an approval can never reach text Parsa was not shown."""
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT rel, hash FROM blocks WHERE source='v' AND id IN ({q})",
            [int(i) for i in ids]).fetchall()
        rows = [(r, h) for r, h in rows if h not in self._approved(r)]
        return self._approve_rows(rows, by=by, via=via)

    def approve_note(self, rel: str, *, by: str, via: str) -> int:
        """Approve all of V's text in one note, as it stands now. Returns how
        many runs were approved.

        Whole blocks are approved by their own hash. Remnants of blocks that
        are no longer whole are recorded as blocks of their own first, so the
        approval still binds to the exact text that was in front of him.
        """
        refuse_in_model_shell()
        text = self._read(rel)
        if text is None:
            return 0
        lines = _lines(text)
        approved = self._approved(rel)
        rows = [(rel, h) for h, btext in self._v_blocks(rel)
                if h not in approved and _contains(lines, _lines(btext))]
        n = self._approve_rows(rows, by=by, via=via) if rows else 0
        for p in self.pending(rel):
            self._insert_hashed(rel, [(block_hash(p.text), p.text)], V,
                                origin="approval")
            n += self._approve_rows([(rel, block_hash(p.text))], by=by, via=via)
        return n

    # ---- backfill from git -----------------------------------------------
    def backfill(self, *, prefix: str = "[bot]", dry_run: bool = False) -> dict:
        """Label the vault as it stands, from `git blame`.

        A line whose last change was a `[bot]` commit is V's, unless that
        commit's subject is one of the model-free writers in `DETERMINISTIC`.
        Every other line is Parsa's — including a line V wrote and he later
        edited, because blame names his commit for it. Reads git, writes only
        this store (nothing at all on a dry run), never a note. Existing
        labels win: a block recorded by a real write knows more than blame.
        """
        root = self.root
        git = ["git", "-C", str(root)]
        names = subprocess.run(
            [*git, "log", "--grep", "^" + _re_escape(prefix), "--name-only",
             "--format="], capture_output=True, text=True, check=False).stdout
        touched = sorted({n.strip() for n in names.splitlines()
                          if n.strip().endswith(".md")
                          and (root / n.strip()).is_file()})
        all_notes = [str(p.relative_to(root)) for p in root.rglob("*.md")
                     if ".git" not in p.parts and ".obsidian" not in p.parts
                     and not any(part.startswith(".") for part in
                                 p.relative_to(root).parts)]
        stats = {"notes": len(all_notes), "bot_touched": len(touched),
                 "v_notes": 0, "v_blocks": 0, "parsa_notes": 0,
                 "parsa_blocks": 0, "v_unapproved": 0, "written": 0}
        touched_set = set(touched)
        for rel in all_notes:
            if rel in touched_set:
                continue
            text = self._read(rel) or ""
            runs = _runs([ln.strip() != "" for ln in _lines(text)])
            if runs:
                stats["parsa_notes"] += 1
                stats["parsa_blocks"] += len(runs)

        for rel in touched:
            per_line = _blame(git, rel, prefix)
            if per_line is None:
                continue
            v_runs, p_runs = [], []
            cur, start = None, 0
            for i, (src, _c, _t, _ln) in enumerate(per_line + [(None, "", "", "")]):
                if src != cur:
                    if cur is not None:
                        (v_runs if cur == V else p_runs).append((start, i))
                    cur, start = src, i
            blocks = []
            for a, b in v_runs:
                text = "\n".join(_trim([ln for *_x, ln in per_line[a:b]]))
                if not text:
                    continue
                _src, commit, at, _ln = per_line[a]
                blocks.append((block_hash(text), text, commit, at))
            p_count = sum(1 for a, b in p_runs
                          if any(ln.strip() for *_x, ln in per_line[a:b]))
            if blocks:
                stats["v_notes"] += 1
                stats["v_blocks"] += len(blocks)
                approved = set() if dry_run else self._approved(rel)
                stats["v_unapproved"] += sum(1 for h, *_ in blocks
                                             if h not in approved)
            if p_count:
                stats["parsa_notes"] += 1
                stats["parsa_blocks"] += p_count
            if blocks and not dry_run:
                for h, text, commit, at in blocks:
                    self._insert_hashed(rel, [(h, text)], V, origin="backfill",
                                        commit=commit, at=at,
                                        keep_existing=True)
                    stats["written"] += 1
        if not dry_run:
            with self.conn as c:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('backfilled_at', ?)",
                          (_now(),))
        return stats


def _contains(hay: list[str], needle: list[str]) -> bool:
    """Whether `needle` occurs in `hay` as a run of whole lines."""
    needle = _trim(needle)
    if not needle:
        return False
    m = len(needle)
    return any(ln == needle[0] and hay[i:i + m] == needle
               for i, ln in enumerate(hay))


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i))
            start = None
    return out


def _re_escape(s: str) -> str:
    import re
    return re.escape(s)


def _blame(git: list[str], rel: str, prefix: str):
    """[(source, commit, author-time, line)] for every line of the working
    copy, or None if git could not blame it. Uncommitted lines are Parsa's:
    `Vault` commits every write it makes, so anything uncommitted was typed."""
    out = subprocess.run([*git, "blame", "--line-porcelain", "--", rel],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return None
    rows = []
    summary: dict[str, str] = {}
    when: dict[str, str] = {}
    sha = None
    for line in out.stdout.split("\n"):
        if line.startswith("\t"):
            subj = summary.get(sha, "")
            is_bot = subj.startswith(prefix) and not any(
                subj[len(prefix):].strip().startswith(d) for d in DETERMINISTIC)
            rows.append((V if is_bot else PARSA, (sha or "")[:12],
                         when.get(sha, ""), line[1:].rstrip()))
            continue
        head = line.split(" ", 1)
        if len(head[0]) == 40 and all(ch in "0123456789abcdef" for ch in head[0]):
            sha = head[0]
        elif head[0] == "summary" and sha:
            summary[sha] = head[1] if len(head) > 1 else ""
        elif head[0] == "author-time" and sha and len(head) > 1:
            try:
                when[sha] = dt.datetime.fromtimestamp(int(head[1])).isoformat(
                    timespec="microseconds")
            except ValueError:
                pass
    return rows


def _smooth(lines: list[str], labels: list[str | None]) -> list[str | None]:
    """A blank line between two runs with the same label joins them, so one
    paragraph-separated block of V's text is fenced once, not per paragraph."""
    out = list(labels)
    n = len(lines)
    for i in range(n):
        if out[i] is None and not lines[i].strip():
            prev = next((out[k] for k in range(i - 1, -1, -1)
                         if lines[k].strip()), None)
            nxt = next((labels[k] for k in range(i + 1, n)
                        if lines[k].strip()), None)
            if prev and prev == nxt:
                out[i] = prev
    return out


def _wrap(lines: list[str], labels: list[str | None]) -> str:
    out, cur = [], None
    for ln, lab in zip(lines, labels):
        if lab != cur:
            if cur:
                out.append(CLOSE)
            if lab:
                out.append(OPEN[lab])
            cur = lab
        out.append(ln)
    if cur:
        out.append(CLOSE)
    return "\n".join(out)


# One store per vault per process; cheap to build, but `Vault` builds often.
_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def store(settings) -> Store:
    key = f"{settings.vault}|{settings.state_dir}"
    with _stores_lock:
        st = _stores.get(key)
        if st is None:
            st = _stores[key] = Store(settings)
        return st
