r"""V's memory. The thing that makes it fit *this* brain rather than any brain.

Hermes was supposed to provide this and didn't, so it's ours. The design goal
is narrow and worth stating: **a memory that gets better because Parsa corrects
it**, not one that accumulates whatever the model felt like saving. Those are
different systems, and the second one degrades — every wrong belief it writes
becomes context it reads back next time, confidently.

So four rules, all enforced here rather than left to a prompt:

1. **A memory is a lesson, not a transcript.** What gets written is a durable
   statement about how V should work or what is true of Parsa's world — never
   a restatement of the request that prompted it.

   This used to be decided by a regex on the incoming message, and that is
   exactly how it failed. `^no[,.\s]` matched "no i want you to actually search
   and document famous vulnerabilities", stored the sentence verbatim as a
   *correction*, and thereafter injected "i want you to actually search and
   document famous vulnerabilities" into every prompt as a standing
   instruction. Two of the two memories the store ever held arrived that way.
   A pattern can see that a message opens with "no"; it cannot see whether what
   follows is a lesson or a retry. So the write decision moved to the model,
   which reads the exchange and calls `vault_memory_write` when there is
   something worth keeping — and `vault_memory_revise` / `vault_memory_forget`
   when what it kept turns out to be wrong. The rules below still bound it:
   the model chooses *what* to store, not whether the store decays, supersedes,
   or keeps its evidence.

2. **Every memory carries its own evidence.** `why` is not decoration — it's
   what lets a later contradiction be judged against the original reason
   instead of just overwriting it.

3. **Contradiction supersedes, it doesn't delete.** A superseded memory stays
   readable with a pointer to what replaced it, because "V used to think X"
   is the single most useful thing when V is behaving strangely.

4. **Unused memories decay.** Strength rises when a memory is used and the turn
   isn't corrected, falls when it's contradicted, and bleeds off slowly when
   nothing touches it. A memory nobody has needed in two months stops being
   injected before it stops existing.

Storage is SQLite (same db as the index) with a Markdown mirror under
`19 - Assistant/`, because a memory Parsa can't read and edit in Obsidian is a
memory he can't trust.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings

# --- kinds -----------------------------------------------------------------
# Deliberately few. Every extra kind is a judgement call at write time, and a
# taxonomy nobody can apply consistently is worse than four blunt buckets.
FACT = "fact"                # about Parsa or his world: people, projects, tools
PREFERENCE = "preference"    # how V should behave: tone, defaults, formatting
CORRECTION = "correction"    # V got something wrong; this is the right answer
PATTERN = "pattern"          # a recurring shape in the work worth anticipating
VOICE = "voice"              # how to *speak* to him; see `core/traits.py`

KINDS = (FACT, PREFERENCE, CORRECTION, PATTERN, VOICE)

# `VOICE` is stored here and rendered somewhere else, which is the one thing
# about it worth knowing. It lives in this table because a learned habit of
# speech is a lesson like any other and wants exactly what this store already
# provides — evidence, supersession, decay, an event log, a Markdown mirror.
# It is rendered by `traits.py` instead of `context_block` because it amends
# the *Voice* section of the system prompt, not the facts, and a habit filed
# under "Facts about him and his work" reads as a claim about Parsa rather
# than an instruction to V. `recall()` and `context_block()` therefore skip
# it; `traits.TraitStore` is the only reader.

# Preferences are behavioural — they belong in the system prompt of *every*
# run, whether or not they look topically relevant, because "be brief" doesn't
# share keywords with the question it applies to.
#
# Corrections used to be in here too, and that was the second half of the
# regex failure: a correction bypassed relevance scoring entirely (10.0 +
# strength, see `recall`), so one bad capture became a standing instruction on
# every call about every topic, under a header that said "Follow them". A
# correction is about a specific thing V got wrong; it earns its place in the
# prompt by matching the message, like a fact does. A correction general enough
# to apply always is a *preference*, and the model is told to write it as one.
ALWAYS_ON = (PREFERENCE,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL DEFAULT 'v',   -- whose belief this is
    kind          TEXT NOT NULL,
    subject       TEXT,            -- the slot this memory occupies; collisions supersede
    text          TEXT NOT NULL,
    why           TEXT,
    source        TEXT,            -- 'told' | 'corrected' | 'observed' | 'seeded'
    confidence    REAL DEFAULT 0.7,
    strength      REAL DEFAULT 1.0,
    used_count    INTEGER DEFAULT 0,
    created       REAL,
    last_used     REAL,
    active        INTEGER DEFAULT 1,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mem_kind    ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_mem_active  ON memories(active);

-- Why a memory has the strength it has. Without this, decay is unexplainable
-- and the first time V forgets something it matters, there is nothing to look at.
CREATE TABLE IF NOT EXISTS memory_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER,
    event     TEXT,     -- created | used | reinforced | contradicted | superseded | decayed
    detail    TEXT,
    at        REAL
);
CREATE INDEX IF NOT EXISTS idx_memev_mem ON memory_events(memory_id);
"""

# Built after the migration below, never inside `SCHEMA`: on a database that
# predates the column, `executescript(SCHEMA)` would reach this line before the
# `ALTER TABLE` had added it and fail the whole script.
INDEXES = "CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(agent);"

# Strength arithmetic. Small numbers on purpose: a memory should need a few
# consistent signals to become load-bearing, and a single contradiction should
# knock it out of the injected set without erasing it.
REINFORCE_STEP = 0.25
CONTRADICT_STEP = 0.60
DECAY_PER_DAY = 0.015          # ~9 weeks of silence to fade a fresh memory out
MIN_INJECT_STRENGTH = 0.35     # below this it stops being injected
MAX_STRENGTH = 3.0

# How much two memories must overlap to be about the same thing. The gap
# between them is the safety margin, and it is deliberately wide:
#   0.71  "always call them merge requests" vs "…, never MRs"  → supersede
#   0.43  "always use Persian dates in the brief" vs "…short titles…" → coexist
# Set the supersede bar too low and unrelated preferences eat each other;
# too high and a correction sits next to the belief it was meant to replace.
SUPERSEDE_THRESHOLD = 0.55
RESTATE_THRESHOLD = 0.85


@dataclass
class Memory:
    id: int
    kind: str
    text: str
    why: str = ""
    agent: str = "v"     # whose belief; see `MemoryStore.scope`
    subject: str = ""
    source: str = "told"
    confidence: float = 0.7
    strength: float = 1.0
    used_count: int = 0
    created: float = 0.0
    last_used: float = 0.0
    active: bool = True
    superseded_by: int | None = None

    def age_days(self, now: float | None = None) -> float:
        return ((now or time.time()) - (self.created or 0)) / 86400

    def effective_strength(self, now: float | None = None) -> float:
        """Strength after decay. Decay is applied on read, not by a cron job —
        a background task that must run for correctness is a background task
        that will one day not have run."""
        now = now or time.time()
        idle = (now - (self.last_used or self.created or now)) / 86400
        return max(0.0, self.strength - idle * DECAY_PER_DAY)

    def render(self) -> str:
        out = f"- {self.text}"
        if self.why:
            out += f"  _(why: {self.why})_"
        return out

    def describe(self) -> str:
        age = f"{self.age_days():.0f}d"
        return (f"[{self.id}] {self.kind}/{self.subject or '—'} · "
                f"strength {self.effective_strength():.2f} · used {self.used_count}× · "
                f"{age} old\n    {self.text}"
                + (f"\n    why: {self.why}" if self.why else ""))


class MemoryStore:
    """One agent's beliefs.

    `scope` is whose they are and is the only thing written; `reads` are other
    agents' scopes this one may see. The split is one-way on purpose — EDITH
    needs to know what V has learned in order to be useful, and must not be
    able to quietly author a belief that V will then act on. Every write in
    this class goes to `self.scope`; nothing writes to a scope in `reads`.

    Rows predating the column belong to `v`: the store had one occupant when
    they were written, and that occupant was V.
    """

    def __init__(self, settings: Settings | None = None,
                 conn: sqlite3.Connection | None = None,
                 embedder=None, scope: str = "v",
                 reads: tuple[str, ...] = ()):
        self.s = settings or default_settings
        if conn is None:
            from . import index
            conn = index.connect(self.s)
        self.conn = conn
        self.conn.executescript(SCHEMA)
        # Older databases predate the column; adding it here keeps an upgrade
        # from needing a rebuild, and the default puts every existing row where
        # it belongs. Same shape as `tasks.ensure_schema`.
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "agent" not in have:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN agent TEXT NOT NULL DEFAULT 'v'")
        self.conn.executescript(INDEXES)
        # Optional and free: the same local embedding backend the vault index
        # already uses. Token overlap alone cannot tell that "put gym stuff in
        # 04 - Gym" and "gym notes belong in 04 - Gym, not the daily note" are
        # one belief — they share three content words out of eight — so
        # paraphrases pile up as separate memories saying the same thing.
        self._embedder = embedder
        self._vec_cache: dict[str, object] = {}
        self.scope = scope or "v"
        # Never read your own scope twice, and keep the order stable so the
        # SQL below is cacheable.
        self.reads = tuple(dict.fromkeys(r for r in reads if r != self.scope))
        self.conn.commit()

    @property
    def _visible(self) -> tuple[str, ...]:
        """Scopes this store may read from, its own first."""
        return (self.scope,) + self.reads

    def _scope_sql(self, column: str = "agent") -> tuple[str, list[str]]:
        marks = ",".join("?" * len(self._visible))
        return f"{column} IN ({marks})", list(self._visible)
        # Optional and free: the same local embedding backend the vault index
        # already uses. Token overlap alone cannot tell that "put gym stuff in
        # 04 - Gym" and "gym notes belong in 04 - Gym, not the daily note" are
        # one belief — they share three content words out of eight — so
        # paraphrases pile up as separate memories saying the same thing.
        self._embedder = embedder
        self._vec_cache: dict[str, object] = {}

    def _similarity(self, a: str, b: str) -> float:
        """How much two memories are about the same thing.

        Max of lexical overlap and embedding cosine: overlap is exact and
        catches identifiers the embedder has never seen, cosine catches
        paraphrase. Taking the max means a miss by either is survivable, which
        matters more here than precision — the cost of a false merge is one
        superseded memory Parsa can read and restate, and the cost of a miss is
        two contradictory beliefs in the prompt at once.
        """
        lexical = _overlap(_tokens(a), _tokens(b))
        if lexical >= RESTATE_THRESHOLD or self._embedder is None:
            return lexical
        try:
            import numpy as np

            va, vb = self._vector(a), self._vector(b)
            if va is None or vb is None:
                return lexical
            cos = float(np.dot(va, vb))
            return max(lexical, cos)
        except Exception:
            return lexical

    def _vector(self, text: str):
        if text in self._vec_cache:
            return self._vec_cache[text]
        try:
            vec = self._embedder.encode([text])[0]
        except Exception:
            vec = None
        self._vec_cache[text] = vec
        return vec

    # ---- writing -----------------------------------------------------
    def remember(self, text: str, *, kind: str = FACT, why: str = "",
                 subject: str | None = None, source: str = "told",
                 confidence: float = 0.7, now: float | None = None) -> Memory:
        """Store a memory, superseding whatever previously held its subject.

        `subject` is the slot. Two memories about how to name merge requests
        should not coexist — the second one is a correction of the first, and
        keeping both means the injected context contradicts itself and V picks
        whichever it read last.
        """
        now = now or time.time()
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("refusing to store an empty memory")
        if kind not in KINDS:
            kind = FACT
        subject = subject or _subject_of(text)

        # Which existing beliefs is this one about? Similarity, not an exact
        # subject key: the first version keyed on the first four content words
        # and "always call them merge requests" and "always call them merge
        # requests, never MRs" hashed to different slots, so the correction
        # sat *alongside* what it corrected instead of replacing it — the
        # exact failure the slot existed to prevent.
        prior = self._related(text, kind)
        prior = [(p, sim) for p, sim in prior if sim >= SUPERSEDE_THRESHOLD]

        for p, sim in prior:
            if sim >= RESTATE_THRESHOLD:
                # A restatement is reinforcement, not a new belief. Without
                # this, every "yes, brief" is another row and the injected
                # block grows until it is most of the prompt.
                self.reinforce(p.id, "restated", now=now)
                # Everything else it competes with still has to go. Returning
                # here unconditionally left the older, near-duplicate phrasing
                # alive alongside it — so the prompt carried two versions of
                # one belief and V announced it had replaced something it
                # hadn't.
                for other, _ in prior:
                    if other.id != p.id:
                        self._supersede(other.id, p.id, now)
                self.conn.commit()
                return self.get(p.id)

        cur = self.conn.execute(
            "INSERT INTO memories (agent,kind,subject,text,why,source,"
            "confidence,strength,used_count,created,last_used,active)"
            " VALUES (?,?,?,?,?,?,?,1.0,0,?,?,1)",
            (self.scope, kind, subject, text, why, source, confidence,
             now, now))
        mid = int(cur.lastrowid)
        self._event(mid, "created", f"{source}: {why}"[:300], now)

        for p, sim in prior:
            self._supersede(p.id, mid, now)
        self.conn.commit()
        return self.get(mid)

    def _related(self, text: str, kind: str) -> list[tuple[Memory, float]]:
        """Active memories of the same kind, ranked by how much they overlap.

        Same-kind only: a *fact* about Kian and a *preference* about how to
        talk about Kian are not competing beliefs, and superseding one with the
        other loses both.
        """
        out = []
        # `own_only`: superseding is a write, and a write never leaves this
        # agent's scope. Without it EDITH — who reads V's memory — would
        # retire V's beliefs as a side effect of forming her own, which is
        # exactly the quiet authorship the read/write split exists to prevent.
        for m in self.all(kind=kind, own_only=True):
            sim = self._similarity(text, m.text)
            if sim > 0:
                out.append((m, sim))
        out.sort(key=lambda kv: -kv[1])
        return out

    def related(self, text: str, *,
                threshold: float = SUPERSEDE_THRESHOLD
                ) -> list[tuple[Memory, float]]:
        """Beliefs a correction is plausibly *about*, across every kind.

        Distinct from `recall()` on purpose. `recall()` is "what should the
        model see", and it hands back behavioural memories whatever the topic —
        correct for prompting, catastrophic for deciding what a correction
        overturns. This one is strictly topical.
        """
        out = []
        for m in self.all(own_only=True):
            sim = self._similarity(text, m.text)
            if sim >= threshold:
                out.append((m, sim))
        out.sort(key=lambda kv: -kv[1])
        return out

    def _supersede(self, old_id: int, new_id: int, now: float) -> None:
        self.conn.execute(
            "UPDATE memories SET active=0, superseded_by=? WHERE id=?",
            (new_id, old_id))
        self._event(old_id, "superseded", f"replaced by #{new_id}", now)

    def forget(self, memory_id: int, reason: str = "asked to") -> bool:
        if not self.owns(memory_id):
            return False
        self.conn.execute("UPDATE memories SET active=0 WHERE id=?", (memory_id,))
        self._event(memory_id, "forgotten", reason, time.time())
        self.conn.commit()
        return True

    # ---- feedback ----------------------------------------------------
    def reinforce(self, memory_id: int, detail: str = "", *,
                  now: float | None = None) -> None:
        # Reading another agent's memory does not strengthen its belief. The
        # count is evidence about *its* store and this agent is only a reader.
        if not self.owns(memory_id):
            return
        now = now or time.time()
        self.conn.execute(
            "UPDATE memories SET strength=MIN(?, strength+?), last_used=? WHERE id=?",
            (MAX_STRENGTH, REINFORCE_STEP, now, memory_id))
        self._event(memory_id, "reinforced", detail, now)
        self.conn.commit()

    def contradict(self, memory_id: int, detail: str = "", *,
                   now: float | None = None) -> None:
        """Parsa says this memory is wrong. Drop its strength hard, but keep it:
        the record of a belief V held and abandoned is worth more than the
        disk it costs."""
        if not self.owns(memory_id):
            return
        now = now or time.time()
        self.conn.execute(
            "UPDATE memories SET strength=MAX(0.0, strength-?) WHERE id=?",
            (CONTRADICT_STEP, memory_id))
        self._event(memory_id, "contradicted", detail, now)
        row = self.conn.execute("SELECT strength FROM memories WHERE id=?",
                                (memory_id,)).fetchone()
        if row and row["strength"] <= 0.0:
            self.conn.execute("UPDATE memories SET active=0 WHERE id=?", (memory_id,))
        self.conn.commit()

    def mark_used(self, ids: list[int], now: float | None = None, *,
                  refresh: bool = True) -> None:
        """Count a use. `refresh=False` counts it without resetting the decay clock.

        That distinction is what makes rule 4 real. `context_block` calls this
        on everything it injects, so with an unconditional `last_used=?` the
        act of being put in a prompt reset the memory's own idle timer — and
        `effective_strength` measures idleness. An always-on memory was
        therefore injected on every call, refreshed on every call, and could
        never decay: strength 1.00 forever, `used 9×` meaning "pasted into nine
        prompts" rather than "helped nine times". Being *shown* to the model is
        not evidence a memory was worth showing; only `reinforce()` — which a
        turn nobody corrected calls — is allowed to move the clock forward.
        """
        now = now or time.time()
        sql = ("UPDATE memories SET used_count=used_count+1, last_used=? WHERE id=?"
               if refresh else
               "UPDATE memories SET used_count=used_count+1 WHERE id=?")
        for mid in ids:
            if not self.owns(mid):
                continue
            self.conn.execute(sql, (now, mid) if refresh else (mid,))
        self.conn.commit()

    def revise(self, memory_id: int, text: str = "", why: str = "", *,
               now: float | None = None) -> Memory | None:
        """Rewrite a memory in place, keeping its id, strength and history.

        The self-editing half of the loop. Superseding is for "V believed X,
        now it believes Y"; this is for "that belief was right but badly
        worded" — the case that used to require forgetting and re-teaching,
        which lost the evidence trail and reset the strength that had been
        earned. The old text goes into the event log, so nothing is lost.
        """
        m = self.get(memory_id)
        # Not `active` means superseded or forgotten, and both are history. A
        # belief V abandoned is only useful as the thing it actually said at
        # the time; editing it rewrites the record that explains the behaviour.
        if not m or not m.active or not (text or why):
            return None
        if not self.owns(memory_id):
            return None
        now = now or time.time()
        new_text, new_why = text or m.text, why or m.why
        self.conn.execute(
            "UPDATE memories SET text=?, why=?, subject=? WHERE id=?",
            (new_text, new_why, _subject_of(new_text), memory_id))
        self._event(memory_id, "revised", f"was: {m.text}"[:300], now)
        self.conn.commit()
        return self.get(memory_id)

    # ---- reading -----------------------------------------------------
    def get(self, memory_id: int) -> Memory | None:
        """Visible rows only. An id another agent owns reads as absent rather
        than as forbidden — there is one person here, and a store that
        distinguishes the two leaks the other agent's contents by id."""
        clause, vals = self._scope_sql()
        row = self.conn.execute(
            f"SELECT * FROM memories WHERE id=? AND {clause}",
            (memory_id, *vals)).fetchone()
        return _row(row) if row else None

    def owns(self, memory_id: int) -> bool:
        """Whether this agent may write to that row, as opposed to read it."""
        row = self.conn.execute(
            "SELECT agent FROM memories WHERE id=?", (memory_id,)).fetchone()
        return bool(row) and row["agent"] == self.scope

    def all(self, *, active_only: bool = True, kind: str | None = None,
            own_only: bool = False) -> list[Memory]:
        """Everything visible to this agent — its own, plus whatever `reads`
        allows. `own_only` narrows to what it may also *write*."""
        sql = "SELECT * FROM memories"
        where, params = [], []
        if own_only:
            where.append("agent=?")
            params.append(self.scope)
        else:
            clause, vals = self._scope_sql()
            where.append(clause)
            params += vals
        if active_only:
            where.append("active=1")
        if kind:
            where.append("kind=?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY strength DESC, last_used DESC"
        return [_row(r) for r in self.conn.execute(sql, params)]

    def recall(self, query: str, limit: int = 6, *,
               now: float | None = None) -> list[Memory]:
        """Memories worth putting in front of the model for *this* message.

        Behavioural memories come along regardless of topic — a preference
        about tone has no words in common with the question it governs. Factual
        ones have to earn their place by matching.
        """
        now = now or time.time()
        tokens = _tokens(query)
        scored: list[tuple[float, Memory]] = []
        for m in self.all():
            if m.kind == VOICE:
                continue          # traits.py selects and renders these
            strength = m.effective_strength(now)
            if strength < MIN_INJECT_STRENGTH:
                continue
            if m.kind in ALWAYS_ON:
                scored.append((10.0 + strength, m))
                continue
            overlap = _overlap(tokens, _tokens(m.text + " " + m.subject))
            if overlap <= 0:
                continue
            scored.append((overlap * 2 + strength, m))
        scored.sort(key=lambda kv: -kv[0])
        return [m for _, m in scored[:limit]]

    def context_block(self, query: str = "", limit: int = 8) -> str:
        """The memory block injected into an agent run. Empty string when there
        is nothing worth saying — an empty section header is still tokens."""
        hits = self.recall(query, limit=limit)
        if not hits:
            return ""
        # Counted, not refreshed — see `mark_used`. Injection is not use.
        self.mark_used([m.id for m in hits], refresh=False)
        lines = ["What you've learned about Parsa and how he wants you to work:"]
        for kind, label in ((PREFERENCE, "How he wants you to work"),
                            (CORRECTION, "Corrections he's made"),
                            (FACT, "Facts about him and his work"),
                            (PATTERN, "Patterns you've noticed")):
            block = [m for m in hits if m.kind == kind]
            if not block:
                continue
            lines.append(f"\n{label}:")
            lines += [m.render() for m in block]
        lines.append(
            "\nYou wrote these yourself, from things Parsa told or corrected "
            "you on. Follow them. If one is wrong, stale, or badly worded, fix "
            "it with `vault_memory_revise` or drop it with "
            "`vault_memory_forget` — say so rather than silently ignoring it.")
        return "\n".join(lines)

    # ---- introspection ------------------------------------------------
    def stats(self) -> dict:
        rows = self.all()
        by_kind: dict[str, int] = {}
        for m in rows:
            by_kind[m.kind] = by_kind.get(m.kind, 0) + 1
        inactive = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE active=0").fetchone()[0]
        faded = sum(1 for m in rows
                    if m.effective_strength() < MIN_INJECT_STRENGTH)
        return {"active": len(rows), "by_kind": by_kind,
                "superseded_or_forgotten": inactive, "faded": faded,
                "strongest": rows[0].text[:80] if rows else None}

    def history(self, memory_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT event, detail, at FROM memory_events WHERE memory_id=?"
            " ORDER BY at", (memory_id,))]

    def _event(self, memory_id: int, event: str, detail: str,
               at: float) -> None:
        self.conn.execute(
            "INSERT INTO memory_events (memory_id,event,detail,at) VALUES (?,?,?,?)",
            (memory_id, event, detail[:500], at))

    # ---- the Obsidian mirror ------------------------------------------
    def export_markdown(self) -> str:
        """Render memory as a note. A memory Parsa can't read is one he can't
        trust, and the vault is where he already looks."""
        rows = self.all()
        now = dt.datetime.now()
        out = [
            "---",
            "type: v-memory",
            f'updated: "{now.date().isoformat()}"',
            f"count: {len(rows)}",
            "tags:",
            "  - assistant",
            "  - memory",
            "---",
            "",
            "# What V Remembers",
            "",
            "_Generated from V's memory store — edit through V (`/forget 4`, "
            "or just correct it in conversation), not by hand; this file is "
            "overwritten._",
            "",
        ]
        for kind, label in ((VOICE, "How I've learned to speak"),
                            (PREFERENCE, "How I should work"),
                            (CORRECTION, "Corrections"),
                            (FACT, "Facts"),
                            (PATTERN, "Patterns")):
            block = [m for m in rows if m.kind == kind]
            if not block:
                continue
            out.append(f"## {label}")
            out.append("")
            for m in block:
                strength = m.effective_strength()
                faded = " ·  _fading_" if strength < MIN_INJECT_STRENGTH else ""
                out.append(f"- **{m.text}**{faded}")
                if m.why:
                    out.append(f"    - why: {m.why}")
                out.append(f"    - `#{m.id}` · {m.kind} · strength "
                           f"{strength:.2f} · used {m.used_count}× · "
                           f"learned {dt.date.fromtimestamp(m.created)}")
            out.append("")

        clause, vals = self._scope_sql()
        gone = [_row(r) for r in self.conn.execute(
            f"SELECT * FROM memories WHERE active=0 AND {clause} "
            "ORDER BY id DESC LIMIT 20", vals)]
        if gone:
            out += ["## No longer believed", "",
                    "_Kept on purpose: when I behave oddly, what I used to "
                    "think is usually the explanation._", ""]
            for m in gone:
                why = (f" → superseded by `#{m.superseded_by}`"
                       if m.superseded_by else " → dropped")
                out.append(f"- ~~{m.text}~~{why}")
            out.append("")
        return "\n".join(out)


# --- memory commands -------------------------------------------------------
# What remains of the pattern layer, and the shape of what's allowed to stay:
# these two match a *command* — a fixed string Parsa types to operate the store
# — not a meaning. `/forget 3` cannot be a request in disguise.
#
# The phrasing patterns that used to sit here (`_CORRECTION_RE`,
# `_PREFERENCE_RE`, `_REMEMBER_RE`) are gone. They were trying to read intent
# off a prefix — "no…", "i want you to…", "always…" — and a prefix does not
# carry intent. Between them they produced two memories, both junk, both
# verbatim restatements of one-off requests. Writing is the model's job now;
# see `vault_memory_write` in `mcp/server.py`.
_FORGET_RE = re.compile(r"^\s*(?:/forget|forget)\s+(?:#)?(\d+)\s*$", re.I)
_MEMORIES_RE = re.compile(
    r"^\s*(?:/memor(?:y|ies)|what do you (?:remember|know) about me)\s*$", re.I)


def read_command(text: str) -> tuple[str, int | None] | None:
    """`('forget', 3)`, `('list', None)`, or None. Commands only — never content."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _FORGET_RE.match(raw)
    if m:
        return ("forget", int(m.group(1)))
    if _MEMORIES_RE.match(raw):
        return ("list", None)
    return None


# --- helpers ---------------------------------------------------------------
_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "is",
    "are", "was", "were", "be", "been", "it", "that", "this", "with", "as",
    "at", "by", "from", "i", "you", "me", "my", "your", "we", "us", "not",
    "do", "does", "did", "so", "if", "then", "than", "when", "what", "how",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[\w؀-ۿ]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _similar(a: str, b: str) -> float:
    return _overlap(_tokens(a), _tokens(b))


def _subject_of(text: str, max_words: int = 4) -> str:
    """A crude slot key: the memory's content words, sorted.

    Sorted deliberately — "never file gym in the daily note" and "gym notes
    never go in the daily note" should collide, and word order is the only
    thing separating them.
    """
    toks = sorted(_tokens(text))[:max_words]
    return " ".join(toks) or (text or "")[:40].lower()


def _clip(text: str, width: int = 120) -> str:
    t = " ".join((text or "").split())
    return t[:width] + ("…" if len(t) > width else "")


def _row(r: sqlite3.Row) -> Memory:
    return Memory(
        id=int(r["id"]), kind=r["kind"], text=r["text"], why=r["why"] or "",
        agent=(r["agent"] if "agent" in r.keys() else None) or "v",
        subject=r["subject"] or "", source=r["source"] or "told",
        confidence=float(r["confidence"] or 0.7),
        strength=float(r["strength"] or 0.0),
        used_count=int(r["used_count"] or 0),
        created=float(r["created"] or 0.0),
        last_used=float(r["last_used"] or 0.0),
        active=bool(r["active"]),
        superseded_by=r["superseded_by"],
    )
