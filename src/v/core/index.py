"""The vault index: SQLite mirror of every note's frontmatter, links, tags and body.

This is what makes structured queries cost $0. Frontmatter fields the vault
already maintains (`status`, `sprint`, `date`, `type`, `rating`, `category`) become
real columns you can `SELECT` on, instead of something an LLM has to read 700K
tokens to answer.

Incremental: only re-parses notes whose mtime changed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import notes, tasks
from .config import Settings, settings as default_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    rel        TEXT PRIMARY KEY,
    title      TEXT,
    type       TEXT,
    folder     TEXT,
    mtime      REAL,
    words      INTEGER,
    meta       TEXT,
    body       TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_type   ON notes(type);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder);

CREATE TABLE IF NOT EXISTS links (
    src TEXT, dst TEXT,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst);

CREATE TABLE IF NOT EXISTS tags (
    rel TEXT, tag TEXT,
    PRIMARY KEY (rel, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- `id` is deterministic: blake2b(rel|ord). It must NOT be an autoincrement
-- rowid, because re-indexing deletes and re-inserts a note's chunks — with
-- autoincrement that silently renumbers them, and every stored embedding then
-- points at the wrong text. Deterministic ids survive a rebuild.
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    rel     TEXT,
    ord     INTEGER,
    heading TEXT,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_rel ON chunks(rel);

CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v TEXT);

-- Checkbox tasks. Same deterministic-id reasoning as chunks: blake2b(rel|line),
-- never a rowid, so re-indexing doesn't renumber a task out from under a
-- reference to it. The schema itself lives in tasks.py beside the parser.


-- Access tracking, the decay signal borrowed from agent-second-brain.
-- Machinery, so it lives here and never in note frontmatter.
CREATE TABLE IF NOT EXISTS access (
    rel           TEXT PRIMARY KEY,
    access_count  INTEGER DEFAULT 0,
    last_access   REAL,
    last_surfaced REAL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    rel UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 0'
);
"""


@dataclass
class IndexStats:
    scanned: int = 0
    updated: int = 0
    removed: int = 0
    chunks: int = 0
    tasks: int = 0
    seconds: float = 0.0
    fts: bool = True

    def __str__(self) -> str:
        return (f"{self.scanned} notes scanned · {self.updated} updated · "
                f"{self.removed} removed · {self.chunks} chunks · "
                f"{self.tasks} tasks · "
                f"{self.seconds:.1f}s" + ("" if self.fts else " · FTS unavailable"))


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    s = settings or default_settings
    s.ensure_dirs()
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    tasks.ensure_schema(conn)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        pass  # built without FTS5; keyword search degrades to LIKE
    conn.commit()
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='notes_fts'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

def chunk_note(note: notes.Note, target_tokens: int = 400, overlap: int = 60
               ) -> list[tuple[int, str, str]]:
    """Split a note into (ord, heading, text) chunks along heading boundaries.

    Tokens are approximated as words * 1.4 for English; Persian runs closer to
    2.5 tokens/word, so the estimate is deliberately conservative.
    """
    body = note.body.strip()
    if not body:
        return []

    sections: list[tuple[str, str]] = []
    matches = list(notes.HEADING.finditer(body))
    if not matches:
        sections.append(("", body))
    else:
        if matches[0].start() > 0:
            head = body[: matches[0].start()].strip()
            if head:
                sections.append(("", head))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            text = body[start:end].strip()
            if text:
                sections.append((m.group(2).strip(), text))

    out: list[tuple[int, str, str]] = []
    n = 0
    for heading, text in sections:
        words = text.split()
        approx = _token_estimate(text)
        if approx <= target_tokens:
            out.append((n, heading, text))
            n += 1
            continue
        # Split long sections by words, with overlap.
        per_chunk = max(50, int(len(words) * target_tokens / max(approx, 1)))
        step = max(1, per_chunk - int(per_chunk * overlap / max(target_tokens, 1)))
        for i in range(0, len(words), step):
            piece = " ".join(words[i: i + per_chunk])
            if piece.strip():
                out.append((n, heading, piece))
                n += 1
            if i + per_chunk >= len(words):
                break
    return out


def chunk_id(rel: str, ord_: int) -> int:
    """Stable id for a chunk. Survives re-indexing, so embeddings stay valid."""
    import hashlib

    h = hashlib.blake2b(f"{rel}\x00{ord_}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") & 0x3FFFFFFFFFFFFFFF


def _token_estimate(text: str) -> int:
    """Rough token count. Persian/Arabic script tokenises far denser than English."""
    persian = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    latin = len(text) - persian
    return int(persian / 1.7 + latin / 4) + 1


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

def rebuild(settings: Settings | None = None, *, full: bool = False,
            progress=None) -> IndexStats:
    s = settings or default_settings
    conn = connect(s)
    stats = IndexStats(fts=has_fts(conn))
    t0 = time.time()

    known = {r["rel"]: r["mtime"] for r in conn.execute("SELECT rel, mtime FROM notes")}
    seen: set[str] = set()

    for path in notes.iter_notes(s.vault):
        rel = str(path.relative_to(s.vault)).replace("\\", "/")
        seen.add(rel)
        stats.scanned += 1
        mtime = path.stat().st_mtime
        if not full and known.get(rel) and abs(known[rel] - mtime) < 1e-6:
            continue
        try:
            note = notes.parse(path, s.vault)
        except (OSError, UnicodeDecodeError):
            continue
        _upsert(conn, note, s, stats)
        stats.updated += 1
        if progress and stats.updated % 50 == 0:
            progress(stats)

    gone = set(known) - seen
    for rel in gone:
        _delete(conn, rel, stats.fts)
        stats.removed += 1

    stats.chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # Recurring-vs-real is a whole-vault judgement (does this checkbox appear in
    # many notes?), so it can only run once every note has been seen.
    tasks.classify_recurring(conn)
    stats.tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind='task'").fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO meta_kv VALUES ('last_index', ?)",
                 (str(time.time()),))
    conn.commit()
    stats.seconds = time.time() - t0
    conn.close()
    return stats


def _upsert(conn: sqlite3.Connection, note: notes.Note, s: Settings,
            stats: IndexStats) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO notes (rel,title,type,folder,mtime,words,meta,body)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (note.rel, note.title, note.type, note.folder, note.mtime,
         note.word_count, json.dumps(note.meta, ensure_ascii=False, default=str),
         note.body),
    )
    conn.execute("DELETE FROM links WHERE src=?", (note.rel,))
    conn.executemany("INSERT OR IGNORE INTO links VALUES (?,?)",
                     [(note.rel, d) for d in set(note.links)])
    conn.execute("DELETE FROM tags WHERE rel=?", (note.rel,))
    conn.executemany("INSERT OR IGNORE INTO tags VALUES (?,?)",
                     [(note.rel, t) for t in note.tags])

    conn.execute("DELETE FROM chunks WHERE rel=?", (note.rel,))
    rows = chunk_note(note, s.chunk_tokens, s.chunk_overlap)
    conn.executemany(
        "INSERT OR REPLACE INTO chunks (id,rel,ord,heading,text) VALUES (?,?,?,?,?)",
        [(chunk_id(note.rel, o), note.rel, o, h, t) for o, h, t in rows],
    )

    try:
        stats.tasks += tasks.replace_for_note(conn, note.rel,
                                              tasks.parse_note(note))
    except (OSError, UnicodeDecodeError):
        # A note that can't be re-read for line numbers still indexes as text;
        # losing its checkboxes is better than losing the note.
        tasks.delete_for_note(conn, note.rel)

    if stats.fts:
        conn.execute("DELETE FROM notes_fts WHERE rel=?", (note.rel,))
        conn.execute("INSERT INTO notes_fts (rel,title,body) VALUES (?,?,?)",
                     (note.rel, note.title, note.body))


def resync_note(conn: sqlite3.Connection, rel: str,
                settings: Settings | None = None) -> int:
    """Re-index exactly one note, for use right after V wrote to it.

    The mirror is the only thing that can see tasks — they are checkbox lines
    in note bodies, so nothing queries the markdown directly — which means a
    write that updates the file but not the mirror produces a task that exists
    on disk and is invisible to every question asked about it. `add()` had
    exactly that shape, and the engine papered over it with a whole-vault
    `rebuild()` (~1,100 stats) while the MCP server did nothing at all.

    Updating `notes.mtime` matters as much as the task rows: leave it stale and
    the file now looks newer than the index to `is_stale()`, so the next
    startup pays for a full rebuild it doesn't need.

    Returns the number of task rows now stored for the note, or -1 if the note
    has since been deleted.
    """
    s = settings or default_settings
    path = s.vault / rel
    if not path.exists():
        _delete(conn, rel, has_fts(conn))
        conn.commit()
        return -1
    stats = IndexStats(fts=has_fts(conn))
    _upsert(conn, notes.parse(path, s.vault), s, stats)
    conn.commit()
    return stats.tasks


def _delete(conn: sqlite3.Connection, rel: str, fts: bool) -> None:
    for table in ("notes", "chunks"):
        conn.execute(f"DELETE FROM {table} WHERE rel=?", (rel,))
    conn.execute("DELETE FROM links WHERE src=?", (rel,))
    conn.execute("DELETE FROM tags WHERE rel=?", (rel,))
    tasks.delete_for_note(conn, rel)
    if fts:
        conn.execute("DELETE FROM notes_fts WHERE rel=?", (rel,))


# ---------------------------------------------------------------------------
# access tracking (decay signal)
# ---------------------------------------------------------------------------

def touch(conn: sqlite3.Connection, rels: list[str]) -> None:
    now = time.time()
    for rel in rels:
        conn.execute(
            "INSERT INTO access (rel, access_count, last_access) VALUES (?,1,?)"
            " ON CONFLICT(rel) DO UPDATE SET"
            " access_count = access_count + 1, last_access = ?",
            (rel, now, now),
        )
    conn.commit()


def last_indexed(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT v FROM meta_kv WHERE k='last_index'").fetchone()
    try:
        return float(row["v"]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def is_stale(settings: Settings | None = None,
             conn: sqlite3.Connection | None = None) -> bool:
    """Has anything in the vault changed since the last index?

    Cheap: one `stat` per note, no parsing. Worth doing on every startup
    because the alternative is what the assistant did before — answer from a
    day-old mirror, with a confident number, and no hint that the note you
    just wrote isn't in it yet.

    Compared per note, against the mtime stored for that note, rather than
    against a single whole-vault `last_index` timestamp. Two reasons. It is
    the same condition `rebuild` uses to decide whether to re-parse a file, so
    "stale" now means exactly "a rebuild would change something" — against the
    global timestamp, a note V had just written and re-indexed by itself still
    read as newer than the last full scan, and every following call ordered a
    full rebuild that did nothing. And a note touched *during* a long rebuild
    used to be permanently invisible: `last_index` is stamped when the scan
    finishes, so the edit looked older than the index that never read it.
    """
    s = settings or default_settings
    own = conn is None
    conn = conn or connect(s)
    try:
        known = {r["rel"]: r["mtime"] for r in
                 conn.execute("SELECT rel, mtime FROM notes")}
        if not known:
            return True
        seen = 0
        for path in notes.iter_notes(s.vault):
            seen += 1
            rel = str(path.relative_to(s.vault)).replace("\\", "/")
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            prev = known.get(rel)
            if prev is None or abs(prev - mtime) >= 1e-6:
                return True
        # A deletion leaves every surviving mtime untouched, so count as well.
        return seen != len(known)
    finally:
        if own:
            conn.close()


def drift(settings: Settings | None = None,
          conn: sqlite3.Connection | None = None) -> list[str]:
    """Which notes a rebuild would touch — the same comparison `is_stale`
    makes, counted instead of short-circuited.

    Separate from `is_stale` rather than expressed as `bool(drift())`, because
    the short-circuit is the point of that one: it stops at the first changed
    note and is run on every startup. This one always walks the whole vault,
    so it is for a surface that wants to *show* the number (`v hud`'s index
    gauge) and is cached by its caller, never for a hot path.
    """
    s = settings or default_settings
    own = conn is None
    conn = conn or connect(s)
    try:
        known = {r["rel"]: r["mtime"] for r in
                 conn.execute("SELECT rel, mtime FROM notes")}
        out, seen = [], set()
        for path in notes.iter_notes(s.vault):
            rel = str(path.relative_to(s.vault)).replace("\\", "/")
            seen.add(rel)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            prev = known.get(rel)
            if prev is None or abs(prev - mtime) >= 1e-6:
                out.append(rel)
        # A deleted note drifts too: it is still in the index and a rebuild
        # would remove it.
        out.extend(sorted(set(known) - seen))
        return out
    finally:
        if own:
            conn.close()


def refresh_if_stale(settings: Settings | None = None,
                     conn: sqlite3.Connection | None = None) -> IndexStats | None:
    """Re-index only when the vault moved. Returns None when nothing was done."""
    if not is_stale(settings, conn):
        return None
    return rebuild(settings)


def stats(settings: Settings | None = None) -> dict:
    s = settings or default_settings
    conn = connect(s)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out = {
        "notes": q("SELECT COUNT(*) FROM notes"),
        "words": q("SELECT COALESCE(SUM(words),0) FROM notes"),
        "chunks": q("SELECT COUNT(*) FROM chunks"),
        "tasks": q("SELECT COUNT(*) FROM tasks"),
        "tasks_open": q("SELECT COUNT(*) FROM tasks WHERE state IN ('open','doing')"),
        "links": q("SELECT COUNT(*) FROM links"),
        "tags": q("SELECT COUNT(DISTINCT tag) FROM tags"),
        "types": {r["type"] or "(none)": r["n"] for r in conn.execute(
            "SELECT type, COUNT(*) n FROM notes GROUP BY type ORDER BY n DESC")},
        "fts": has_fts(conn),
        "db": str(s.db_path),
        "db_bytes": s.db_path.stat().st_size if s.db_path.exists() else 0,
    }
    conn.close()
    return out
