"""v-mcp — the vault, as tools V's reasoning shell can call.

Run it:  python3 -m v.mcp.server        (stdio)

Design notes worth keeping:

**The cheap path stays cheap inside the agent.** `vault_query` is a SELECT over
frontmatter: exact, instant, free, and incapable of inventing a note that does
not exist. Its description tells the model to reach for it before grepping,
because a model that greps 616 files to answer "which issues are in-review"
is paying real money to do worse than `query.py` does for nothing.

**There is no raw write tool, and that is the point.** The shell is never
granted Write or Edit on the vault. Every mutation goes through `vault_append`
or `vault_create`, which are `vaultfs` calls, which are append-only, git
committed with a `[bot]` prefix, and revertible. That guarantee holds no matter
what the model decides it wants to do.

**Privacy is enforced here, not upstream.** See `guard.py`.

**`vault_chart`/`vault_trend`/`vault_map` don't return a chart — they open
one.** The shell only ever gets text back from this whole process (see
`shells.ClaudeCodeShell.run`), so there's no channel to hand a picture
through. These tools do the entire job themselves: build a self-contained
HTML file (`core/render.py`), save it, and open it in the browser. The model
never sees the HTML and should not try to describe its contents — it just
reports what it opened.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time

from mcp.server.fastmcp import FastMCP

from ..core import calendar as calendar_mod
from ..core import mapsearch
from ..core import memory as memory_mod
from ..core import persona as persona_mod
from ..core import traits as traits_mod
from ..core import (contacts, index, notes, nudges as nudges_mod, query, rag,
                    reminders, render, schema, work)
from ..core.config import settings as S
from ..core.vaultfs import Vault, VaultError
from .guard import Blocked, Privacy

# --- engines, one bundle per thread -----------------------------------------
# The MCP runtime dispatches tool calls on worker threads, and a sqlite3
# connection may only be used on the thread that opened it. Rather than share
# one connection behind a lock, give each thread its own bundle: connections
# are cheap, the vault index is read-mostly, and this keeps concurrent tool
# calls genuinely concurrent instead of serialising them behind a mutex.
_local = threading.local()
_vault = Vault(S)          # no sqlite; safe to share
_work = work.Work(S, _vault)
_privacy = Privacy(S)


class _Engines:
    __slots__ = ("conn", "q", "rag", "nudges", "reminders", "memory",
                 "traits")

    def __init__(self) -> None:
        self.conn = index.connect(S)
        self.q = query.QueryEngine(S, self.conn)
        self.rag = rag.Rag(S, self.conn)
        self.nudges = nudges_mod.NudgeEngine(S, self.q)
        self.reminders = reminders.Reminders(S, self.conn)
        # Which agent this server is answering for. Set by `shells.py` on the
        # subprocess it launches; absent when a client starts `v-mcp` directly,
        # which is V's own surface and so is the right default.
        agent = persona_mod.get(os.environ.get("V_AGENT", ""))
        self.memory = memory_mod.MemoryStore(
            S, self.conn, scope=agent.scope, reads=agent.reads_scopes)
        self.traits = traits_mod.TraitStore(self.memory, agent)


# --- keeping the mirror fresh -----------------------------------------------
# Tasks, unlike issues or workouts, exist only in the SQLite mirror: they are
# checkbox lines inside note bodies, so every question about them is answered
# from `tasks`, never from the markdown. That makes an unsynced mirror not a
# slightly-stale answer but a wrong one — a task ticked in Obsidian still
# listed as open, a task written this morning reported as not existing.
#
# The CLI gets away without this because `Engine.__init__` refreshes and the
# process then exits. This server is long-lived: it connected once, at startup,
# and every tool call since has read whatever the vault looked like then.
#
# `is_stale` is one stat() per note (~30ms across this vault), which is too
# much per tool call and nothing at all per turn — hence the throttle. The lock
# keeps two worker threads from rebuilding over each other.
_FRESH_EVERY = 3.0          # seconds; shorter than any human turn
_fresh_lock = threading.Lock()
_fresh_at = 0.0


def _ensure_fresh() -> None:
    global _fresh_at
    now = time.monotonic()
    if now - _fresh_at < _FRESH_EVERY:
        return
    with _fresh_lock:
        if time.monotonic() - _fresh_at < _FRESH_EVERY:
            return
        try:
            index.refresh_if_stale(S)
        except Exception:
            # A mirror one turn out of date beats a tool call that errors.
            pass
        _fresh_at = time.monotonic()


def E() -> _Engines:
    _ensure_fresh()
    e = getattr(_local, "engines", None)
    if e is None:
        e = _Engines()
        _local.engines = e
    return e

mcp = FastMCP(
    name="v-vault",
    instructions=(
        "Tools over Parsa's Obsidian vault at ~/Documents/Brain, for V.\n\n"
        "ALWAYS prefer `vault_query` over Grep/Glob for anything with a "
        "frontmatter schema — issues, MRs, workouts, places, assignments, "
        "goals, people. It is exact and free; grepping is neither.\n"
        "Use `vault_search` for questions about note *bodies* — decisions, "
        "ideas, prose. Use Grep only when both of those come up empty.\n\n"
        "Tasks are `- [ ]` checkboxes in note bodies, not frontmatter — use "
        "`vault_tasks`, never grep. The calendar is a vault file at "
        "'01 - Daily Notes/Calendar/YYYY-MM.md' and is the source of truth; "
        "read it with `vault_calendar`.\n\n"
        "You cannot write to the vault directly. `vault_append`, "
        "`vault_create`, `vault_add_task`, `vault_task_done` and "
        "`vault_schedule` are the only write paths; all commit to git and are "
        "undoable via `vault_undo`."
    ),
)


# ---------------------------------------------------------------- helpers --
def _rows_out(result: query.Result, limit: int) -> str:
    """Render a Result, dropping anything private, and say so if we dropped."""
    before = len(result.rows)
    result.rows = [r for r in result.rows if not _privacy.blocks(r.rel)]
    out = f"**{result.answer}**"
    if result.rows:
        out += "\n\n```\n" + result.table(limit) + "\n```"
        out += "\n\nNotes: " + " ".join(result.citations()[:10])
    out += _privacy.redacted(before, len(result.rows))
    return out


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def _reminders():
    """The outbox, opened per call.

    Not cached: `Reminders` holds a sqlite3 connection, this server answers on
    a thread pool, and a connection may only be used from the thread that
    opened it — the same rule `_Bridge` exists for on the phone transport. One
    connection per `vault_send_file` costs a file open on a database this
    process touches a handful of times a day.
    """
    from ..core.reminders import Reminders
    return Reminders(S)


def _send_to_telegram(path, *, caption: str = "") -> bool:
    """Best-effort push of a rendered file to Telegram, alongside the local
    browser tab `open_file()` already opened. Local import, same reason
    `briefing.py` does it: a vault with no bot configured must not pay for
    importing `core.telegram` (and everything it pulls in via `core.engine`)
    on every chart request, and `send_file` degrades to a silent no-op
    anyway when there's no token to send with."""
    from ..core import telegram
    return telegram.send_file(S, path, caption=caption)


# ------------------------------------------------------------------ reads --
QUERY_KINDS = {
    "issues", "merge_requests", "stale_reviews", "blocked", "workouts",
    "last_workout", "places", "people", "overdue_contacts", "birthdays",
    "assignments", "goals", "dormant_goals", "on_this_day", "orphans",
    "broken_links", "untyped", "empty_notes", "daily_notes",
}


@mcp.tool(
    description=(
        "Structured query over note frontmatter. Exact, instant, $0, and "
        "cannot hallucinate. CALL THIS FIRST for anything with a schema: "
        "issues by status, open MRs, recent workouts, cafés with wifi, "
        "assignments due, quarterly goals, birthdays, people you've lost "
        "touch with, orphan notes, broken wikilinks.\n\n"
        "kind must be one of: " + ", ".join(sorted(QUERY_KINDS)) + ".\n"
        "Optional filters (ignored where they don't apply): status, project, "
        "sprint, category, min_rating, wifi, outdoor, open_late.\n\n"
        "For kind='places', `near` + `radius_km` rank/filter by distance "
        "instead of rating: `near` is a place note's name ('Home', 'Divar', "
        "any note under 09 - .../Locations) or a raw 'lat,lng' string. "
        "'cafés within 2km of Divar' is category='cafe', near='Divar', "
        "radius_km=2. Omit radius_km to just rank by distance with no cutoff."
    )
)
def vault_query(
    kind: str,
    status: str | None = None,
    project: str | None = None,
    sprint: str | None = None,
    category: str | None = None,
    min_rating: int | None = None,
    wifi: bool | None = None,
    outdoor: bool | None = None,
    open_late: bool | None = None,
    near: str | None = None,
    radius_km: float | None = None,
    limit: int = 20,
) -> str:
    if kind not in QUERY_KINDS:
        return _err(f"unknown kind {kind!r}. Valid: {', '.join(sorted(QUERY_KINDS))}")
    try:
        if kind == "issues":
            r = E().q.issues(status=status, project=project, sprint=sprint)
        elif kind == "merge_requests":
            r = E().q.merge_requests(status=status)
        elif kind == "places":
            r = E().q.places(category=category, min_rating=min_rating,
                             wifi=wifi, outdoor=outdoor, open_late=open_late,
                             near=near, radius_km=radius_km)
        elif kind == "assignments":
            r = E().q.assignments(status=status)
        else:
            r = getattr(E().q, kind)()
    except TypeError:
        r = getattr(E().q, kind)()
    except Exception as exc:                       # a bad filter shouldn't 500
        return _err(f"{kind} failed: {exc}")
    return _rows_out(r, limit)


@mcp.tool(
    description=(
        "Hybrid semantic + keyword search over note BODIES. Use for questions "
        "with no exact schema answer — 'what did I decide about pricing', "
        "'where did I write about Kafka consumers', lyric fragments, ideas. "
        "Returns the matching passages with their source notes, so you can "
        "cite or vault_read the full note. Free."
    )
)
def vault_search(query_text: str, k: int = 8) -> str:
    res = E().rag.search(query_text, k=k)
    if not res.hits:
        extra = "" if E().rag.ready else " (semantic index not built — run `index --embed`)"
        return f"No matches for {query_text!r}.{extra}"
    hits = [h for h in res.hits if not _privacy.blocks(h.rel)]
    lines = [f"{len(hits)} matches ({res.backend}, {res.ms:.0f}ms):", ""]
    for h in hits:
        where = h.rel[:-3] + (f" › {h.heading}" if h.heading else "")
        lines.append(f"• **{where}**  _{h.score:.2f}_\n  {h.preview(300)}")
    return "\n".join(lines) + _privacy.redacted(len(res.hits), len(hits))


@mcp.tool(
    description=(
        "Read one note in full, frontmatter included. Use after vault_search "
        "or vault_query hands you a path you need the whole of. Path is "
        "vault-relative, e.g. '02 - Work/Issues/ISSUE-139 Percentage discount.md'."
    )
)
def vault_read(rel: str) -> str:
    try:
        _privacy.check(rel)
    except Blocked as exc:
        return _err(str(exc))
    try:
        note = _vault.read(rel)
    except VaultError as exc:
        return _err(str(exc))
    meta = json.dumps(note.meta, ensure_ascii=False, indent=2) if note.meta else "{}"
    return f"# {rel}\n\n## frontmatter\n```json\n{meta}\n```\n\n## body\n{note.body}"


@mcp.tool(
    description=(
        "Everything the vault currently knows is wrong or overdue: 14 checks "
        "including gym gaps, stale reviews, assignments due, dormant quarterly "
        "goals, birthdays, schema errors, broken links, unrated places. Free. "
        "Call this for 'what needs me', 'what am I forgetting', or when "
        "building a morning brief."
    )
)
def vault_nudges(max_items: int = 12, min_urgency: int = 0) -> str:
    items = _privacy.scrub_nudges(E().nudges.all())
    return nudges_mod.digest(items, max_items=max_items, min_urgency=min_urgency)


@mcp.tool(
    description=(
        "Validate every note against the frontmatter schemas in CLAUDE.md that "
        "the Dataview dashboards depend on. Reports notes that are silently "
        "invisible to a dashboard because of a bad `status` value or a missing "
        "field. Call before trusting a count, or when a dashboard looks empty."
    )
)
def vault_validate(limit: int = 25) -> str:
    report = schema.validate_vault(S)
    lines = [f"**{report.summary()}**", ""]
    for issue in report.errors[:limit]:
        lines.append(f"- {issue}")
    if len(report.errors) > limit:
        lines.append(f"… {len(report.errors) - limit} more")
    return "\n".join(lines)


@mcp.tool(
    description="Index health, semantic backend, git state, pending reminders. "
                "Call when something looks stale or a search returns nothing."
)
def vault_status() -> str:
    st = index.stats(S)
    rg = E().rag.info()
    sem = (f"{rg['backend']} · {rg['chunks']} chunks · dim {rg['dim']}"
           if rg.get("built") else "NOT BUILT")
    return "\n".join([
        f"vault: {S.vault}",
        f"notes: {st['notes']} ({st['words']:,} words, {st['chunks']} chunks)",
        f"semantic index: {sem}",
        f"keyword search (FTS5): {'yes' if st['fts'] else 'no'}",
        f"git: {'on' if _vault.git_available else 'off'}",
        f"reminders pending: {len(E().reminders.pending())}",
        f"private folders (never exposed here): {', '.join(_privacy.folders)}",
        f"  · one exception: vault_contact reads emails and phone numbers "
        f"out of {S.folders.crm}, and nothing else from it",
    ])


@mcp.tool(
    description="Yesterday's commits, today's open work, and blockers — the "
                "standup, assembled from git and issue frontmatter. Free."
)
def vault_standup() -> str:
    return _work.standup()


@mcp.tool(
    description=(
        "Checkbox tasks — every `- [ ]` in the vault, parsed and ranked. "
        "USE THIS instead of grepping for '- [ ]'. Tasks live in note bodies, "
        "not frontmatter, so vault_query cannot see them.\n\n"
        "view:\n"
        "  priorities — what to do next, ranked (default)\n"
        "  open       — all open commitments\n"
        "  overdue    — past their date\n"
        "  backlog    — the counts behind 'how far behind am I'\n"
        "  search     — find a task by text (pass `text`)\n"
        "  ignored    — what Parsa has told V to stop bringing up\n\n"
        "Only real commitments are returned. Template checklists (MR review "
        "boxes), lesson-note bullets, and rituals repeated in every daily note "
        "are classified out; a task copied across several days is collapsed to "
        "one. Tasks Parsa has ignored are left out of every view except "
        "`ignored` and `search` — that is deliberate, not a gap, and their "
        "`- [ ]` lines are still in the notes. `day` is YYYY-MM-DD and "
        "defaults to today."
    )
)
def vault_tasks(view: str = "priorities", text: str | None = None,
                area: str | None = None, day: str | None = None,
                limit: int = 15) -> str:
    e = E()
    when = notes.as_date(day) if day else dt.date.today()
    if view == "backlog":
        r = e.q.backlog(when)
        return r.answer + "\n\n" + r.table(20)
    if view == "ignored":
        rows = e.q.tasks.ignored()
        if not rows:
            return "Nothing is ignored — V is tracking every task."
        lines = [f"{len(rows)} ignored task{'s' if len(rows) != 1 else ''} — "
                 "left out of every other view on purpose. "
                 "vault_task_done(state='unignored') brings one back.", ""]
        for i in rows:
            live = f"hiding {i.live} open" if i.live else "no live copy left"
            where = f" — {i.link()}" if i.rel else ""
            lines.append(f"- {i.text} ({i.age_days()}d ago · {live}){where}")
        return "\n".join(lines)
    if view == "search":
        if not text:
            return _err("view='search' needs `text`")
        hits = e.q.find_task(text, open_only=False)
        if not hits:
            return f"No task matches {text!r}."
        return "\n".join(
            f"- [{t.id}] {t.render(today=when)} — {t.link()} (match {sc:.2f})"
            for t, sc in hits)
    kinds = {"priorities": lambda: e.q.priorities(when, limit=limit, area=area),
             "open": lambda: e.q.open_tasks(area=area, limit=limit),
             "overdue": lambda: e.q.overdue_tasks(when, limit=limit)}
    if view not in kinds:
        return _err(f"unknown view {view!r}. Valid: "
                    "priorities, open, overdue, backlog, search, ignored")
    r = kinds[view]()
    if not r.rows:
        return r.answer
    lines = [r.answer, ""]
    for row in r.rows:
        t = row.extra["_task"]
        lines.append(f"- [{t.id}] {t.render(today=when)} — {t.link()}")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Calendar events from the vault's own calendar at "
        "'01 - Daily Notes/Calendar/YYYY-MM.md', which is the single source of "
        "truth for what Parsa has scheduled. Daily notes render their "
        "'## 📅 Calendar' section from it.\n\n"
        "`day` is YYYY-MM-DD (default today); `days` widens the window. "
        "An empty result means nothing is scheduled — it does NOT mean there "
        "is no calendar, and it is never a reason to say you have no calendar "
        "access."
    )
)
def vault_calendar(day: str | None = None, days: int = 1) -> str:
    when = notes.as_date(day) if day else dt.date.today()
    if when is None:
        return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
    r = E().q.events(when, days=days)
    return r.answer + ("\n\n" + r.table(30) if r.rows else "")


# ------------------------------------------------------------------- the week --
# Three read tools and one write, and they are deliberately not folded into the
# calendar tools above. The calendar answers "what is scheduled"; these answer
# "where do my hours go" — which is a different file, a different question, and
# the only one of the two that can say what is *free*.
@mcp.tool(
    description=(
        "Parsa's hour-by-hour schedule — where his time actually goes, from "
        "the '## Schedule' section of that week's note at "
        "'01 - Daily Notes/Weekly/YYYY/YYYY-Www.md'. One-off events from the "
        "vault calendar are folded in, so this is the complete picture of a "
        "day.\n\n"
        "Each block has a kind: class · work · commute · gym · sleep · study · "
        "paper · personal · event. Use this to answer 'am I in class now?', "
        "'what does Tuesday look like?', 'how much am I working this week?'\n\n"
        "`day` is YYYY-MM-DD (default today); `days` widens the window (7 for "
        "the whole week). An empty result means that day is unscheduled, not "
        "that there is no schedule."
    )
)
def vault_schedule_week(day: str | None = None, days: int = 1) -> str:
    when = notes.as_date(day) if day else dt.date.today()
    if when is None:
        return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
    r = E().q.schedule(when, days=days)
    return r.answer + ("\n\n" + r.table(50) if r.rows else "")


@mcp.tool(
    description=(
        "When Parsa is FREE — the gaps left after class, work, commute, gym, "
        "sleep and every calendar event are subtracted from the waking day. "
        "Nothing in the vault records free time; it is computed here.\n\n"
        "This is the tool for 'when can I work on the paper?', 'do I have two "
        "hours tomorrow?', 'when should we meet?'. Prefer it over reading the "
        "schedule and doing the arithmetic yourself.\n\n"
        "`day` is YYYY-MM-DD (default today), `days` widens the window, and "
        "`min_minutes` sets the shortest gap worth reporting."
    )
)
def vault_free_time(day: str | None = None, days: int = 1,
                    min_minutes: int = 30) -> str:
    when = notes.as_date(day) if day else dt.date.today()
    if when is None:
        return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
    r = E().q.free_time(when, days=days, min_minutes=min_minutes)
    return r.answer + ("\n\n" + r.table(50) if r.rows else "")


@mcp.tool(
    description=(
        "What Parsa is in the middle of right now, and how long until he is "
        "free. Answers 'is he busy?', 'can he talk?' without reading a whole "
        "day's schedule."
    )
)
def vault_now() -> str:
    wp = E().q.weekplan
    now = dt.datetime.now()
    block = wp.now_block(now)
    if block:
        out = f"now ({now:%H:%M}) · {block.render()}"
        gap = wp.next_free(15, start=now)
        if gap and gap.date == now.date():
            out += f"\nfree from {gap.start:%H:%M}"
        return out
    gap = next((g for g in wp.free(now.date(), min_minutes=15,
                                   after=now.time())), None)
    if gap:
        return (f"now ({now:%H:%M}) · nothing scheduled — free until "
                f"{gap.end:%H:%M}")
    return f"now ({now:%H:%M}) · nothing scheduled"


@mcp.tool(
    description=(
        "Add a time block to a week's schedule — 'gym tuesday 19:00-20:30', "
        "'class 2026-09-20 10:30-12:00 Compiler @204', 'paper friday "
        "10:00-13:00'. Use this for TIME (a stretch of the day that is spoken "
        "for), and vault_add_task for a TODO.\n\n"
        "Recognises a kind (class · work · commute · gym · sleep · study · "
        "paper · personal · event) anywhere in the text; the rest becomes the "
        "label. Appends to that week's note only — other weeks are untouched, "
        "which is the point of the design. Commits as [bot] and is undoable. "
        "For something that should repeat every week, tell Parsa to add it to "
        "'05 - Templates/Week Schedule.md' instead."
    )
)
def vault_add_block(text: str) -> str:
    from ..core import weekplan as wp_mod
    block = wp_mod.parse_natural(text, today=dt.date.today())
    if not block:
        return _err("couldn't find a time in that. Try "
                    "'gym tuesday 19:00-20:30'.")
    e = E()
    said = e.q.weekplan.add(block)
    e.q.weekplan.sync_daily_note(block.date)
    return f"{block.date.isoformat()} · {said}"


# The two verbs that change a block that already exists. They are *edits*,
# and they are deliberately not behind `Persona.edit_surfaces` with
# `vault_edit`/`vault_rewrite`: those take arbitrary text and can touch any
# line of any note, which is why they are gated to the machine Parsa is
# sitting at. These take a date and a time, can only ever rewrite one line in
# one `## Schedule` section, validate the kind against a closed vocabulary
# before writing, and refuse a line they cannot find exactly once. "My class
# was cancelled" is the single most obvious thing to say to V from a phone,
# and it would be absurd for that to be the one thing it cannot do.
@mcp.tool(
    description=(
        "Remove a time block from Parsa's schedule — a cancelled class, a "
        "meeting that is off, a gym session he skipped.\n\n"
        "`day` is the date the block is on (YYYY-MM-DD) and `at` is its start "
        "time (HH:MM). Call vault_schedule_week first if you are not certain "
        "of both — this refuses rather than guessing, and a wrong guess "
        "deletes the wrong hour.\n\n"
        "Writes one line out of that week's note, commits as [bot], and is "
        "undoable with vault_undo. To cancel something only this once, this "
        "is the right tool; to stop it repeating every week, tell Parsa to "
        "edit '05 - Templates/Week Schedule.md' instead."
    )
)
def vault_remove_block(day: str, at: str) -> str:
    e = E()
    when = notes.as_date(day)
    if when is None:
        return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
    found = _block_at(e, when, at)
    if isinstance(found, str):
        return found
    try:
        return e.q.weekplan.remove(when, found.line())
    except Exception as exc:
        return _err(str(exc))
    finally:
        e.q.weekplan.sync_daily_note(when)


@mcp.tool(
    description=(
        "Move or change a time block that is already on the schedule — push a "
        "meeting an hour later, shift a gym session to another day, rename it, "
        "or change its kind.\n\n"
        "`day` + `at` name the block as it stands now (call vault_schedule_week "
        "if unsure). Everything else is optional and only what you pass is "
        "changed: `to_day` (YYYY-MM-DD), `to_start`/`to_end` (HH:MM), `kind` "
        "(class · work · commute · gym · sleep · study · paper · personal · "
        "event), `label`.\n\n"
        "Rewrites the line in place so the note stays in time order, commits "
        "as [bot], and is undoable with vault_undo."
    )
)
def vault_move_block(day: str, at: str, to_day: str | None = None,
                     to_start: str | None = None, to_end: str | None = None,
                     kind: str | None = None, label: str | None = None) -> str:
    from ..core import weekplan as wp_mod
    e = E()
    when = notes.as_date(day)
    if when is None:
        return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
    found = _block_at(e, when, at)
    if isinstance(found, str):
        return found

    changes: dict = {}
    if to_day:
        moved = notes.as_date(to_day)
        if moved is None:
            return _err(f"couldn't read {to_day!r} as a date — use YYYY-MM-DD")
        changes["date"] = moved
    for key, raw in (("start", to_start), ("end", to_end)):
        if raw:
            parsed = wp_mod._parse_time(raw)
            if parsed is None:
                return _err(f"couldn't read {raw!r} as a time — use HH:MM")
            changes[key] = parsed
    if kind:
        changes["kind"] = kind
    if label is not None:
        changes["label"] = label
    if not changes:
        return _err("nothing to change — pass to_day, to_start, to_end, "
                    "kind or label")
    try:
        said = e.q.weekplan.move(when, found.line(), **changes)
    except Exception as exc:
        return _err(str(exc))
    e.q.weekplan.sync_daily_note(when)
    if changes.get("date") and changes["date"] != when:
        e.q.weekplan.sync_daily_note(changes["date"])
    return said


def _block_at(engine, day: dt.date, at: str):
    """The one block starting at `at` on `day`, or an error string saying why not.

    Addressed by start time rather than by its written line, because a model
    holding a whole line is a model that has to reproduce it byte-perfectly —
    including the `·` separators and an `@location` it may not have been shown.
    A time is what Parsa would say out loud.
    """
    from ..core import weekplan as wp_mod
    want = wp_mod._parse_time(at or "")
    if want is None:
        return _err(f"couldn't read {at!r} as a time — use HH:MM")
    on_day = [b for b in engine.q.weekplan.on(day) if b.start == want]
    if not on_day:
        listed = ", ".join(f"{b.time_label} {b.title}"
                           for b in engine.q.weekplan.on(day)) or "nothing"
        return _err(f"nothing starts at {at} on {day.isoformat()}. "
                    f"That day has: {listed}.")
    if len(on_day) > 1:
        listed = ", ".join(f"{b.kind} {b.title}" for b in on_day)
        return _err(f"{len(on_day)} blocks start at {at} on {day.isoformat()} "
                    f"({listed}) — this tool can't tell which you mean.")
    return on_day[0]


# ------------------------------------------------------------------ contacts --
# The one tool that reads `10 - Personal CRM/`, and it returns addresses and
# numbers only — `core/contacts.py` documents why that hole exists and why it
# is this shape. Everything else in this file stays blind to the folder.
@mcp.tool(
    description=(
        "Look up how to reach someone: email addresses and phone numbers "
        "from Parsa's contacts (`10 - Personal CRM/`).\n\n"
        "Use this BEFORE asking Parsa for someone's email or number. He "
        "wrote these down so he would not have to type them again; asking "
        "him for one he has already recorded is the failure this tool "
        "exists to prevent.\n\n"
        "Contact details ONLY. The rest of a CRM note — what he thinks of "
        "someone, what they are going through, gift ideas, the interaction "
        "log — is private and is not readable by you through this or any "
        "other tool. Do not try to reach it another way; report that it is "
        "private if asked.\n\n"
        "Several people can match one first name; you get all of them, so "
        "ask which rather than picking."
    )
)
def vault_contact(name: str) -> str:
    found = contacts.lookup(name, S)
    if not found:
        return (f"No contact matching {name!r} in {S.folders.crm}. "
                f"Match is on whole names, so try the full one.")
    lines = []
    for c in found:
        bits = []
        if c.emails:
            bits.append("email: " + ", ".join(c.emails))
        if c.phones:
            bits.append("phone: " + ", ".join(c.phones))
        lines.append(f"- {c.name} — " + ("; ".join(bits) if bits else
                                         "no address or number on the note"))
    head = f"{len(found)} contact(s) matching {name!r}:"
    return head + "\n" + "\n".join(lines)


# ----------------------------------------------------------- Google Calendar --
# The live account, as opposed to `vault_calendar`, which is what Parsa has
# written down. Both are real and they answer different questions: the vault
# calendar is the source of truth for his own notes, this is where invitations
# arrive and where writing something is what makes other people see it.
def _gcal():
    """Reading works off the iCal feeds already in `.gcal_ics_url` and needs
    no OAuth; only writing does. Gating both behind `configured()` made the
    tools claim there was no calendar while two feeds sat there working."""
    from ..core import gcal as gcal_mod
    g = gcal_mod.GoogleCalendar(S)
    if not g.configured() and not gcal_mod.ical_feeds(S):
        raise gcal_mod.NotConfigured(
            "No Google Calendar configured — neither an iCal feed in "
            "`.gcal_ics_url` nor an OAuth client. `python3 -m v.cli "
            "gcal auth` prints the setup. vault_calendar needs neither.")
    return g


def _gcal_err(exc) -> str:
    from ..core import gcal as gcal_mod
    if isinstance(exc, gcal_mod.NotConfigured):
        return _err(str(exc))
    if isinstance(exc, gcal_mod.NotAuthorised):
        return _err(f"{exc}\nRun `python3 -m v.cli gcal auth`.")
    return _err(str(exc))


def _gmail():
    from ..core import gmail as gmail_mod
    return gmail_mod.Gmail(S)


def _gmail_err(exc) -> str:
    from ..core import gmail as gmail_mod
    if isinstance(exc, gmail_mod.NotConfigured):
        return _err(str(exc))
    if isinstance(exc, gmail_mod.NotAuthorised):
        return _err(f"{exc}\nRun `python3 -m v.cli gmail auth`.")
    return _err(str(exc))


@mcp.tool(
    description=(
        "Read Google Calendar — Parsa's own, or another person's.\n\n"
        "This is the LIVE account, separate from vault_calendar. Use it when "
        "asked about someone else's schedule, about invitations, or about "
        "anything that would not be in his notes.\n\n"
        "`who` is a person's name ('arash'), an email, or 'me' (default). A "
        "name is matched first against the calendars this account can see, "
        "then against the addresses in Parsa's contacts — so just pass the "
        "name and never ask him for an email address you have not tried to "
        "look up. If it matches several you get all of them, so ask which one "
        "rather than picking.\n\n"
        "What you can actually see about other people, and it matters:\n"
        "  · calendars they shared — full titles and times\n"
        "  · everyone else — busy/free blocks only, no titles, via "
        "`busy_only`. 'Busy 14:00-15:00' is all Google returns; do not invent "
        "a title for it.\n"
        "An empty result for another person usually means they never shared "
        "their calendar, NOT that their day is empty. Say which."
    )
)
def gcal_events(who: str = "me", day: str | None = None, days: int = 1,
                busy_only: bool = False, limit: int = 50) -> str:
    try:
        g = _gcal()
        when = notes.as_date(day) if day else dt.date.today()
        if when is None:
            return _err(f"couldn't read {day!r} as a date — use YYYY-MM-DD")
        matches = g.resolve(who)
        if not matches:
            names = ", ".join(f"{c['name']} <{c['id']}>"
                              for c in g.calendars()[:15]) or "(none)"
            oauth = g.configured() and g.authorised()
            how = ("pass their email with busy_only=true for busy/free blocks"
                   if oauth else
                   "ask them for the 'secret address in iCal format' from "
                   "their Google Calendar settings and add it to "
                   "`.gcal_ics_url` under a `# Name` line — that needs no "
                   "OAuth and works immediately")
            # Distinguish "who?" from "them, but no address on file". Most of
            # the CRM predates anyone recording emails in it, so this is the
            # common miss, and "I don't know that person" would be false.
            known = [c.name for c in contacts.lookup(who, S) if not c.emails]
            if known:
                return (f"{', '.join(known)} is in Parsa's contacts but the "
                        f"note has no email address on it, and Google needs "
                        f"one. Adding an `Email:` line to that note makes "
                        f"this work from the name alone next time.")
            return (f"No calendar matches {who!r}, and no contact of that "
                    f"name has an address on file. Visible right now: "
                    f"{names}.\nAnother person's calendar is only readable "
                    f"here if they shared it with this account. To add one: "
                    f"{how}.")
        if len(matches) > 1:
            listed = "\n".join(f"- {c['name']} <{c['id']}> ({c['access']})"
                               for c in matches)
            return (f"{who!r} matches {len(matches)} calendars — ask which:\n"
                    f"{listed}")

        cal = matches[0]
        start = dt.datetime.combine(when, dt.time.min, tzinfo=S.tz)
        end = start + dt.timedelta(days=max(1, days))

        # `unshared` covers both an address typed in and one resolved from
        # contacts: Google will answer freeBusy for an address it has never
        # shared with us, and nothing else.
        via = (f" (via {cal['id']}, from Parsa's contacts)"
               if cal.get("source") == "crm" else "")
        if busy_only or cal["access"] in ("freeBusyReader", "unshared"):
            blocks = g.freebusy([cal["id"]], start, end).get(cal["id"], [])
            if blocks and blocks[0][0] == "error":
                return (f"Cannot see {cal['name']}'s calendar{via} "
                        f"({blocks[0][1]}) — they have not shared it with "
                        f"this account. That is not the same as a free day.")
            if not blocks:
                return (f"No busy blocks returned for {cal['name']}{via} "
                        f"({when}). Either the day is genuinely free or "
                        f"nothing is shared with this account — Google does "
                        f"not distinguish the two.")
            lines = [f"{cal['name']}{via} — busy blocks "
                     f"(no titles available):"]
            lines += [f"- {a:%Y-%m-%d %H:%M}–{b:%H:%M}" for a, b in blocks]
            return "\n".join(lines)

        evs = g.events(calendar_id=cal["id"], start=start, end=end, limit=limit)
        if not evs:
            return (f"Nothing on {cal['name']}'s calendar for "
                    f"{when.isoformat()}"
                    + (f" +{days - 1}d" if days > 1 else "") + ".")
        lines = [f"{cal['name']} — {len(evs)} event(s):"]
        for e in evs:
            date = e.start if e.all_day else e.start.date()
            lines.append(f"- {date.isoformat()} {e.render()}")
        return "\n".join(lines)
    except Exception as exc:
        return _gcal_err(exc)


@mcp.tool(
    description=(
        "List the Google calendars this account can see — Parsa's own and "
        "every one someone has shared with him. `access` says whether writing "
        "will work: owner/writer yes, reader/freeBusyReader no. Use this "
        "before claiming a person's calendar is unavailable."
    )
)
def gcal_calendars() -> str:
    try:
        cals = _gcal().calendars()
    except Exception as exc:
        return _gcal_err(exc)
    if not cals:
        return "This account can see no calendars at all."
    lines = [f"{len(cals)} calendar(s):"]
    for c in sorted(cals, key=lambda c: (not c["primary"], c["name"])):
        tag = " (yours)" if c["primary"] else ""
        lines.append(f"- {c['name']} <{c['id']}> — {c['access']}{tag}")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Create an event on Google Calendar. WRITES to the live account and "
        "emails the guests, so confirm the details with Parsa before calling "
        "it — unlike vault writes, this one is not undoable with vault_undo.\n\n"
        "`start` and `end` are 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD' with "
        "all_day). `end` defaults to an hour after `start`.\n\n"
        "To put something on someone ELSE's day: leave `who` as 'me' and put "
        "their address in `attendees` — that invites them, which works "
        "without any permission on their calendar. Writing directly into "
        "their calendar via `who` only works if they granted write access."
    )
)
def gcal_add_event(summary: str, start: str, end: str | None = None,
                   who: str = "me", location: str = "",
                   description: str = "", attendees: str = "",
                   all_day: bool = False, notify: bool = True) -> str:
    try:
        g = _gcal()
        matches = g.resolve(who)
        if not matches:
            return _err(f"no calendar matches {who!r} — call gcal_calendars.")
        if len(matches) > 1:
            return _err(f"{who!r} is ambiguous: "
                        + ", ".join(c["id"] for c in matches))
        cal = matches[0]
        if cal.get("source") == "ical":
            return _err(
                f"{cal['name']} is an iCal feed. Feeds are read-only — there "
                f"is no endpoint behind a secret iCal URL to create an event "
                f"through. Writing needs the Calendar API: Parsa runs "
                f"`python3 -m v.cli gcal auth` once. Until then you "
                f"can still read every feed, and vault_schedule still writes "
                f"to his vault calendar.")
        if not cal["writable"] and not cal["primary"]:
            guests = [a for a in attendees.split(",") if a.strip()]
            return _err(
                f"no write access to {cal['name']} (access: {cal['access']}). "
                f"Invite them instead: gcal_add_event(..., who='me', "
                f"attendees='{cal['id']}'"
                + (f", plus {len(guests)} others" if guests else "") + ").")

        s0 = _when(start)
        s1 = _when(end) if end else None
        if s0 is None:
            return _err(f"couldn't read {start!r} — use 'YYYY-MM-DD HH:MM'")
        if end and s1 is None:
            return _err(f"couldn't read {end!r} — use 'YYYY-MM-DD HH:MM'")

        ev = g.add_event(
            summary, start=s0, end=s1, calendar_id=cal["id"],
            location=location, description=description,
            attendees=[a.strip() for a in attendees.split(",") if a.strip()],
            all_day=all_day, notify=notify)
        who_else = (f" · invited {', '.join(ev.attendees)}"
                    if ev.attendees else "")
        return (f"Created on {cal['name']}: {ev.render()}{who_else}\n"
                f"{ev.link}\nEvent id `{ev.id}` — this is live on Google, "
                f"vault_undo does not cover it.")
    except Exception as exc:
        return _gcal_err(exc)


def _when(text: str | None):
    """'2026-08-08 12:30' or '2026-08-08'. Deliberately strict — a misread
    date on a live calendar emails the wrong time to real people."""
    if not text:
        return None
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.date() if fmt == "%Y-%m-%d" else parsed
    return None


@mcp.tool(
    description=(
        "Search Gmail. `query` is Gmail's own search syntax verbatim — "
        "'is:unread', 'from:arash@...', 'newer_than:2d', 'subject:invoice', "
        "combined however Gmail allows. Returns id, sender, subject, snippet "
        "and read state for each hit; call gmail_read for the full body."
    )
)
def gmail_search(query: str, limit: int = 10) -> str:
    try:
        hits = _gmail().search(query, limit=limit)
    except Exception as exc:
        return _gmail_err(exc)
    if not hits:
        return f"No messages matching {query!r}."
    lines = [f"{len(hits)} message(s) matching {query!r}:"]
    lines += [f"- `{h.id}` {h.render()} — {h.snippet}" for h in hits]
    return "\n".join(lines)


@mcp.tool(description="Read one Gmail message in full, by the id gmail_search returned.")
def gmail_read(message_id: str) -> str:
    try:
        m = _gmail().read(message_id)
    except Exception as exc:
        return _gmail_err(exc)
    when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "(no date)"
    return (f"From: {m.from_}\nTo: {m.to}\nSubject: {m.subject or '(no subject)'}\n"
            f"Date: {when}\n\n{m.body}")


@mcp.tool(
    description=(
        "Draft a Gmail reply or new message — creates a DRAFT only, sends "
        "nothing. Pass `thread_id` (from gmail_search/gmail_read) to reply "
        "in-thread, or omit it for a new message. Returns the draft id and "
        "the exact text: show Parsa that text and wait for him to confirm "
        "before ever calling gmail_send with this id. Never call gmail_send "
        "in the same turn as this — a sent email cannot be recalled, unlike "
        "every write in this vault."
    )
)
def gmail_draft(to: str, subject: str, body: str, thread_id: str | None = None) -> str:
    try:
        draft_id = _gmail().draft(to, subject, body, thread_id=thread_id)
    except Exception as exc:
        return _gmail_err(exc)
    return (f"Draft `{draft_id}` created, NOT sent:\n\n"
            f"To: {to}\nSubject: {subject}\n\n{body}\n\n"
            f"Show this to Parsa. Only call gmail_send('{draft_id}') after "
            f"he confirms — do not send it in this same turn.")


@mcp.tool(
    description=(
        "Send a Gmail draft by id. WRITES — actually leaves the account, "
        "irreversibly, unlike vault writes. Only call this after Parsa has "
        "seen the exact draft text (from gmail_draft's return value) and "
        "confirmed it in his own message — never in the same turn that "
        "created the draft, and never for a draft id you invented or recall "
        "from an earlier conversation without re-showing it first."
    )
)
def gmail_send(draft_id: str) -> str:
    try:
        message_id = _gmail().send(draft_id)
    except Exception as exc:
        return _gmail_err(exc)
    return f"Sent. Message id `{message_id}`."


@mcp.tool(description="List pending reminders with their due times.")
def vault_reminders() -> str:
    pend = E().reminders.pending()
    if not pend:
        return "No pending reminders."
    return "\n".join(
        f"- [{r.id}] {r.due.strftime('%a %d %b %H:%M')} — {r.text}"
        + (f" (every {r.recurrence})" if r.recurrence else "")
        for r in pend
    )


@mcp.tool(
    description=(
        "Render a bar or line chart from numbers you already have (parsed "
        "out of a vault_query table, or stated directly by Parsa) as a "
        "self-contained HTML file, opened in the browser. Returns a short "
        "confirmation — say what you opened, don't also describe the "
        "chart's shape in prose. If the numbers are one of the vault's known "
        "series (weight, macros, category spend), use vault_trend instead — "
        "it fetches them itself, so nothing has to round-trip through you."
    )
)
def vault_chart(labels: list[str], values: list[float], kind: str = "bar",
                title: str = "", unit: str = "") -> str:
    if kind not in ("bar", "line"):
        return _err("kind must be 'bar' or 'line'")
    try:
        if kind == "bar":
            page = render.chart_bar_html(labels, values, title=title, unit=unit)
        else:
            page = render.chart_line_html(list(zip(labels, values)),
                                          title=title, unit=unit)
    except ValueError as exc:
        return _err(str(exc))
    path = render.save(page, slug=title or kind, settings=S)
    render.open_file(path)
    sent = _send_to_telegram(path, caption=title)
    return (f"Opened a {kind} chart"
           f"{f' — {title}' if title else ''} in your browser ({path})."
           + (" Sent it to Telegram too." if sent else ""))


_TREND_MACROS = ("calories", "protein", "carbs", "fiber")


@mcp.tool(
    description=(
        "Fetch and chart a known trend in one call, opened in the browser: "
        "'weight' (bodyweight over `days`), 'calories' / 'protein' / 'carbs' "
        "/ 'fiber' (daily macro totals over `days`), or 'category-spend' "
        "(spend by category for the latest — or `month`'s, as Jalali "
        "'YYYY-MM' — Category Snapshot note, the banking app's own "
        "category breakdown). Prefer this over vault_chart whenever the "
        "series is one of these; it fetches the numbers itself."
    )
)
def vault_trend(series: str, days: int = 14, month: str | None = None) -> str:
    series = (series or "").strip().lower()
    try:
        if series == "weight":
            rows = E().q.bodyweight_trend(days).rows
            if not rows:
                return f"No weigh-ins in the last {days} days."
            points = [(str(r.get("date"))[5:], float(r.get("weight-kg")))
                     for r in rows]
            page = render.chart_line_html(points, title="Weight trend", unit="kg")
        elif series in _TREND_MACROS:
            today = dt.date.today()
            points = []
            for i in range(days - 1, -1, -1):
                day = today - dt.timedelta(days=i)
                points.append((day.isoformat()[5:],
                               E().q.macro_totals(day)[series]))
            page = render.chart_line_html(points, title=f"{series.title()} trend")
        elif series == "category-spend":
            result = E().q.category_spend(month)
            if result is None:
                return "No Category Snapshot notes found yet."
            picked_month, totals = result
            if not totals:
                return f"No spend recorded for {picked_month}."
            page = render.chart_bar_html(
                list(totals.keys()), list(totals.values()),
                title=f"Spend by category — {picked_month}", unit="toman")
        else:
            return _err("series must be one of: weight, calories, protein, "
                        "carbs, fiber, category-spend")
    except ValueError as exc:
        return _err(str(exc))
    path = render.save(page, slug=series, settings=S)
    render.open_file(path)
    sent = _send_to_telegram(path, caption=f"{series} trend")
    return (f"Opened the {series} trend in your browser ({path})."
           + (" Sent it to Telegram too." if sent else ""))


@mcp.tool(
    description=(
        "Map of places (same filters as vault_query kind='places': "
        "category, min_rating, city, near, radius_km) as an inline-SVG "
        "scatter — no map-tile server, works offline — opened in the "
        "browser. Places with no recorded `location` are skipped."
    )
)
def vault_map(category: str | None = None, min_rating: int | None = None,
             city: str | None = None, near: str | None = None,
             radius_km: float | None = None) -> str:
    result = E().q.places(category=category, min_rating=min_rating, city=city,
                          near=near, radius_km=radius_km)
    points = []
    for r in result.rows:
        loc = r.get("location")
        if not loc:
            continue
        try:
            lat_s, lng_s = str(loc).split(",")
            lat, lng = float(lat_s.strip()), float(lng_s.strip())
        except (ValueError, AttributeError):
            continue
        points.append({"label": r.title, "lat": lat, "lng": lng,
                       "category": r.get("category"), "rating": r.get("rating")})
    if not points:
        return "No places with a recorded location matched."
    # Live tiles when there's a web key, the offline SVG scatter when there
    # isn't — the second is worse to look at but works with no network, so
    # it stays the fallback rather than being deleted.
    if S.neshan_web_key:
        page = render.place_html(points, web_key=S.neshan_web_key, title="Places")
    else:
        page = render.map_html(points, title="Places")
    path = render.save(page, slug="places-map", settings=S)
    render.open_file(path)
    sent = _send_to_telegram(path, caption="Places")
    return (f"Opened a map of {len(points)} places in your browser ({path})."
           + (" Sent it to Telegram too." if sent else ""))


@mcp.tool(
    description=(
        "How long it takes to get from one place to another, and a map of "
        "the route, opened in the browser. `origin` and `destination` are "
        "each a place note's name ('Divar', 'Home', 'Rooberoo Mansion "
        "Tajrish') or a raw 'lat,lng' — same resolution as vault_query's "
        "`near`. Returns the distance and time as text AND opens the drawn "
        "route, so give the numbers in your reply and say the map is open; "
        "don't describe the route's shape.\n\n"
        "`mode` is 'car' or 'motorcycle'. THERE IS NO TRANSIT OR WALKING "
        "MODE — Neshan does not offer one. If Parsa asks how long by metro "
        "or on foot, say plainly that you can't route it and that the number "
        "you're giving is an estimate; never pass it off as a routing "
        "result. Tehran's طرح ترافیک and زوج/فرد restrictions are avoided by "
        "default, so the route returned is one he can actually drive.\n\n"
        "If this reports no API key configured, say the distance is an "
        "estimate rather than quietly guessing a number that looks measured."
    )
)
def vault_route(origin: str, destination: str, mode: str = "car") -> str:
    a = E().q.resolve_anchor(origin)
    if a is None:
        return _err(f"don't know where {origin!r} is — no place note by that "
                    "name and it doesn't parse as \"lat,lng\" either.")
    b = E().q.resolve_anchor(destination)
    if b is None:
        return _err(f"don't know where {destination!r} is — no place note by "
                    "that name and it doesn't parse as \"lat,lng\" either.")
    o_lat, o_lng, o_label = a
    d_lat, d_lng, d_label = b
    try:
        route = mapsearch.route((o_lat, o_lng), (d_lat, d_lng),
                                origin_label=o_label, dest_label=d_label,
                                mode=mode, settings=S)
    except mapsearch.MapSearchError as exc:
        return _err(str(exc))
    if route is None:
        return (f"Routing isn't configured (no NESHAN_API_KEY), so there is "
                f"no measured time from {o_label} to {d_label}. Say any "
                f"number you give is an estimate, and don't call it a "
                f"routing result.")

    # Three surfaces, three artifacts, because no single one serves all of
    # them. The browser gets a real interactive map (Google's — neshan.org
    # cannot be deep-linked, see `mapsearch.directions_url`). Telegram gets a
    # PNG, which it shows inline where an .html would arrive as a download.
    slug = f"route-{o_label}-{d_label}"
    render.open_url(mapsearch.directions_url((o_lat, o_lng), (d_lat, d_lng),
                                             mode=mode))
    try:
        png = render.route_png(route, settings=S)
    except mapsearch.MapSearchError:
        png = None                      # static-map service not on the key
    sent = False
    if png:
        png_path = render.save_bytes(png, slug=slug, settings=S)
        sent = _send_to_telegram(
            png_path, caption=f"{o_label} → {d_label} · {route.distance_km} km, "
                              f"{route.duration_min} min")

    via = f" via {route.summary}" if route.summary else ""
    return (f"{o_label} → {d_label}: {route.distance_km} km, "
            f"{route.duration_min} min by {route.mode}{via} (current "
            f"traffic, avoiding طرح ترافیک and زوج/فرد). Opened it on the "
            f"map."
           + (" Sent it to Telegram too." if sent else ""))


@mcp.tool(
    description=(
        "Live search for a kind of place NOT already in the vault — 'a good "
        "café near Divar I haven't been to', 'coffee shops around Sharif'. "
        "Different from vault_query kind='places', which only ever answers "
        "from notes Parsa has already written; this hits Neshan's real POI "
        "search around an anchor point, so it can surface places with no "
        "note yet. `anchor` is the same as vault_query's `near` — a place "
        "note's name ('Home', 'Divar') or a raw 'lat,lng'. `term` is what "
        "to search for, in Persian or English ('کافه', 'restaurant').\n\n"
        "If this returns 'not configured', no Neshan API key is set — fall "
        "back to WebSearch/WebFetch against Google Maps or Neshan's site "
        "yourself for the same question, then say plainly that the results "
        "came from the web, not the vault.\n\n"
        "A hit here is NOT a vault note — nothing is saved. If Parsa wants "
        "one kept, use vault_create with type: place (see the Place "
        "template) after he confirms, never automatically."
    )
)
def vault_places_search(term: str, anchor: str, limit: int = 8) -> str:
    resolved = E().q.resolve_anchor(anchor)
    if resolved is None:
        return _err(f"don't know where {anchor!r} is — no place note by that "
                    "name and it doesn't parse as \"lat,lng\" either.")
    lat, lng, label = resolved
    try:
        hits = mapsearch.search(term, lat, lng, settings=S, limit=limit)
    except mapsearch.MapSearchError as exc:
        return _err(str(exc))
    if hits is None:
        return (f"Live map search isn't configured (no NESHAN_API_KEY). Use "
                f"WebSearch/WebFetch against Google Maps or Neshan for "
                f"{term!r} near {label} instead, and say the results are "
                f"from the web, not the vault.")
    if not hits:
        return f"No live results for {term!r} near {label}."
    lines = [f"**{len(hits)} live results for {term!r} near {label}** "
            "(from the web, not the vault):"]
    for h in hits:
        addr = f" — {h.address}" if h.address else ""
        cat = f" [{h.category}]" if h.category else ""
        lines.append(f"- {h.title}{cat}{addr} · {h.distance_km}km · "
                     f"{h.lat},{h.lng}")
    return "\n".join(lines)


# A bot upload, not a user's browser session — Telegram's own cap for
# `sendDocument`/`sendPhoto` via the Bot API, not something this vault
# chooses.
_TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024


@mcp.tool(
    description=(
        "Send a file that already exists in the vault to Parsa — a filled "
        "timesheet, a rendered .pdf or .docx, a screenshot someone dropped in "
        "an attachments folder. Different from vault_chart/vault_trend/"
        "vault_map, which build and send a chart this process just rendered; "
        "this hands over something already sitting on disk. Takes a "
        "vault-relative path, same as every other tool here. Goes to whichever "
        "surfaces are reachable: the phone always (it is queued, so it arrives "
        "even if the phone is asleep), and Telegram too when a bot is "
        "configured. Fails with an explanation — not silently — if the path "
        "doesn't exist or is in a private folder."
    )
)
def vault_send_file(rel_path: str, caption: str = "") -> str:
    try:
        _privacy.check(rel_path)
    except Blocked as exc:
        return _err(str(exc))
    try:
        path = _vault.path(rel_path)
    except VaultError as exc:
        return _err(str(exc))
    if not path.exists() or not path.is_file():
        return _err(f"no such file: {rel_path}")
    size = path.stat().st_size

    # The phone first, and unconditionally. This is a *queue* — a row in the
    # outbox that `v-serve` drains onto the event stream, or that the next chat
    # turn picks up and hands over with its answer — so unlike the Telegram
    # call below it cannot fail because something is asleep. It is also the
    # only path that does not put the file through a third party, which is why
    # it is not the fallback.
    sent = []
    try:
        _reminders().queue("file", json.dumps({"rel": rel_path,
                                               "caption": caption}))
        sent.append("the phone")
    except Exception as exc:            # noqa: BLE001
        return _err(f"could not queue {rel_path}: {exc}")

    # Telegram second, inline, best-effort. Inline because it needs no socket
    # from this process to Parsa — it is an HTTP call to Telegram's own API,
    # which any process can make — and best-effort because a file has already
    # been delivered by the time we get here.
    if S.telegram_bot_token and S.telegram_owner_id:
        if size > _TELEGRAM_FILE_LIMIT:
            sent.append(f"but not Telegram ({size / 1_000_000:.1f} MB is over "
                        f"its {_TELEGRAM_FILE_LIMIT // 1_000_000} MB bot limit)")
        elif _send_to_telegram(path, caption=caption):
            sent.append("Telegram")
    return f"Sent {rel_path} to {' and '.join(sent)}."


# ----------------------------------------------------------------- writes --
@mcp.tool(
    description=(
        "Append text to a note, creating it if absent. THE ONLY WAY TO WRITE "
        "PROSE TO THE VAULT — never use Write or Edit on a .md file here. "
        "Append-only by design: this cannot overwrite anything the user wrote. "
        "Commits to git with a [bot] prefix and returns the commit sha, which "
        "vault_undo takes. Pass `heading` to append under a specific `## "
        "Section`, creating it if missing."
    )
)
def vault_append(rel: str, text: str, heading: str | None = None,
                 summary: str | None = None) -> str:
    try:
        _privacy.check(rel)
        change = _vault.append(rel, text, heading=heading, summary=summary)
    except (Blocked, VaultError) as exc:
        return _err(str(exc))
    return (f"{change.kind} {rel}"
            + (f" under ## {heading}" if heading else "")
            + f"\ncommit: {change.commit or '(git off)'}"
            + f"\nundo with: vault_undo(commit='{change.commit}')")


@mcp.tool(
    description=(
        "Create a NEW note with frontmatter, validated against the CLAUDE.md "
        "schema for its type before anything is written. Refuses to overwrite "
        "an existing note. Use for issues, MRs, places, workouts, sprints — "
        "anything a dashboard queries. `frontmatter` is a JSON object string."
    )
)
def vault_create(rel: str, frontmatter: str, body: str = "") -> str:
    try:
        _privacy.check(rel)
        meta = json.loads(frontmatter)
    except Blocked as exc:
        return _err(str(exc))
    except json.JSONDecodeError as exc:
        return _err(f"frontmatter is not valid JSON: {exc}")
    if not isinstance(meta, dict):
        return _err("frontmatter must be a JSON object")

    # Validate BEFORE writing. A note that fails its schema is invisible to the
    # dashboards that query it, which is a silent failure — so it never lands.
    probe = notes.Note(rel=rel, path=_vault.path(rel), meta=meta, body=body)
    problems = [i for i in schema.validate_note(probe, settings=S)
                if getattr(i, "level", "error") == "error"]
    if problems:
        return _err("schema violations, nothing written:\n"
                    + "\n".join(f"- {p}" for p in problems))

    try:
        change = _vault.create(rel, meta, body)
    except VaultError as exc:
        return _err(str(exc))
    return f"created {rel}\ncommit: {change.commit or '(git off)'}"


@mcp.tool(
    description=(
        "Change a checkbox's state, in the note where it lives. Get `task_id` "
        "from vault_tasks — never guess one. Changes exactly one line "
        "(verified before writing), commits as [bot], and is undoable with "
        "vault_undo. This is the only way to close a task; Write and Edit are "
        "denied.\n\n"
        "state: done (default) · dropped (abandoned — struck through, not "
        "deleted, so the record of having decided survives) · doing · open · "
        "ignored · unignored.\n\n"
        "`ignored` is the one that writes nothing to the vault: the checkbox "
        "stays exactly as Parsa typed it and V stops counting, ranking or "
        "mentioning it anywhere. Use it when he says a task is no longer "
        "worth talking about, is stale, or should stop being brought up — "
        "never `done`, which claims he did it, and prefer it over `dropped` "
        "unless he wants the note itself to record that he gave up. "
        "`unignored` undoes it."
    )
)
def vault_task_done(task_id: int, state: str = "done") -> str:
    import v.core.tasks as tasks_mod
    valid = {"done": tasks_mod.DONE, "dropped": tasks_mod.DROPPED,
             "doing": tasks_mod.DOING, "open": tasks_mod.OPEN}
    if state not in valid and state not in ("ignored", "unignored"):
        return _err(f"unknown state {state!r}. Valid: "
                    f"{', '.join(list(valid) + ['ignored', 'unignored'])}")
    e = E()
    t = e.q.tasks.get(task_id)
    if not t:
        return _err(f"no task with id {task_id}. Use vault_tasks(view='search') "
                    "to find it — ids come from there.")
    if state == "ignored":
        if e.q.tasks.is_ignored(t):
            return f"Already ignored: {t.text} — {t.link()}"
        e.q.tasks.ignore(t, reason="asked")
        return (f"Ignored: {t.text} — {t.link()}. The note is unchanged; it "
                f"just stops appearing in priorities, overdue and the "
                f"backlog. Say the words again to bring it back.")
    if state == "unignored":
        if not e.q.tasks.unignore(t.norm_key):
            return f"Wasn't ignored: {t.text} — {t.link()}"
        return f"Back in the lists: {t.text} — {t.link()}"
    if t.state == valid[state]:
        return f"Already {state}: {t.text} — {t.link()}"
    try:
        line = e.q.tasks.complete(t, _vault, state=valid[state],
                                  today=dt.date.today())
    except (ValueError, OSError) as exc:
        return _err(str(exc))
    return f"Set to {state}: `{line}` in {t.link()}. Undoable with vault_undo."


@mcp.tool(
    description=(
        "Add an event to the vault calendar, the source of truth at "
        "'01 - Daily Notes/Calendar/YYYY-MM.md'. Takes plain language: "
        "'on Aug 8 at 12:30 1on1 with Sajad', 'tomorrow 3pm dentist'. "
        "Appends (never rewrites), commits as [bot], refreshes that day's "
        "note, and is undoable. Duplicates are detected and skipped."
    )
)
def vault_schedule(text: str) -> str:
    ev = calendar_mod.parse_natural(text, today=dt.date.today())
    if not ev:
        return _err("couldn't find a date and time in that. Try "
                    "'on Aug 8 at 12:30 — meeting with Sajad'.")
    e = E()
    said = e.q.calendar.add(ev)
    e.q.calendar.sync_daily_note(ev.date)
    return f"{ev.date.isoformat()} · {said}"


@mcp.tool(
    description=(
        "Add a checkbox task to a note. `rel` defaults to today's daily note; "
        "`heading` picks the section (e.g. 'Tasks'). Append-only and undoable. "
        "`due` is YYYY-MM-DD."
    )
)
def vault_add_task(text: str, rel: str | None = None,
                   heading: str | None = None, due: str | None = None) -> str:
    e = E()
    if rel and _privacy.blocks(rel):
        return _err(f"{rel} is in a private folder")
    target = rel or _today_note_rel()
    try:
        line = e.q.tasks.add(text, _vault, rel=target, heading=heading,
                             due=notes.as_date(due) if due else None)
    except Exception as exc:
        return _err(str(exc))
    return f"Added to {target}: {line}"


def _today_note_rel() -> str:
    d = dt.date.today()
    return (f"01 - Daily Notes/{d.year}/{d.strftime('%m - %B')}/"
            f"{d.isoformat()}.md")


@mcp.tool(
    description=(
        "Set a reminder from natural language — 'call mom tomorrow at 9', "
        "'every day at 7', 'in 20 minutes'. Stored in SQLite, not the vault, "
        "and it survives a restart: anything due while the process was down "
        "still fires on boot."
    )
)
def vault_remind(text: str) -> str:
    rem = E().reminders.add_natural(text, now=dt.datetime.now())
    if not rem:
        return _err("no time found in that. Try 'in 20 minutes', "
                    "'tomorrow at 9', or 'every day at 7'.")
    rec = f", repeating every {rem.recurrence}" if rem.recurrence else ""
    return f"[{rem.id}] {rem.due.strftime('%a %d %b at %H:%M')}{rec} — {rem.text}"


@mcp.tool(
    description=(
        "Revert a vault write. With no argument, reverts the most recent [bot] "
        "commit. Pass a sha to revert a specific one. This is `git revert`, so "
        "it is always safe and always available."
    )
)
def vault_undo(commit: str | None = None) -> str:
    try:
        return _vault.undo(commit)
    except VaultError as exc:
        return _err(str(exc))


# --- memory: the store V writes to itself ----------------------------------
# These four replace a regex that decided a message was worth remembering
# because it started with "no". Judging that is the whole difficulty, and it is
# a judgement about meaning, so it is yours. The store still owns the mechanics
# you should not be trusted with — decay, superseding, keeping the evidence of
# a belief you abandoned — and it is append-and-supersede, so a bad write is
# recoverable rather than destructive.


def _sync_memory_note(store) -> None:
    """Mirror the store into `19 - Assistant/V/Memory.md`. Best-effort."""
    try:
        from ..core import journal as journal_mod
        journal_mod.Journal(S, _vault).write_memory(store.export_markdown())
    except Exception:
        pass


@mcp.tool(
    description=(
        "What you already remember about Parsa. Read this before writing a new "
        "memory so you revise instead of duplicating. Shows id, kind, strength "
        "and use count; `include_dropped` also lists what you used to believe."
    )
)
def vault_memory_list(include_dropped: bool = False) -> str:
    store = E().memory
    rows = store.all(active_only=not include_dropped)
    if not rows:
        return "Nothing remembered yet."
    return "\n".join(m.describe() for m in rows)


@mcp.tool(
    description=(
        "Remember something durable about Parsa or about how you should work. "
        "\n\nWrite a LESSON, never the request that prompted it. 'Parsa wants "
        "vulnerability topics researched and written up, not filed as a task' "
        "is a memory; 'i want you to actually search and document famous "
        "vulnerabilities' is the request restated, and storing that means "
        "reading it back as a standing instruction on every later call. If you "
        "cannot state it as something that will still be true next month, do "
        "not store it.\n\n"
        "kind: 'preference' (how to behave — injected into every run, so keep "
        "these few and general), 'fact' (about him, his projects, his people), "
        "'correction' (a specific thing you got wrong — injected only when the "
        "topic matches), 'pattern' (a recurring shape in the work). `why` is "
        "the evidence: what he said or did that justifies it. Superseding is "
        "automatic — a memory about the same subject retires the older one."
    )
)
def vault_memory_write(text: str, kind: str = "fact", why: str = "",
                       subject: str | None = None) -> str:
    from ..core import memory as memory_mod
    if kind not in memory_mod.KINDS:
        return _err(f"kind must be one of {', '.join(memory_mod.KINDS)}")
    store = E().memory
    before = {m.id for m in store.all()}
    try:
        mem = store.remember(text, kind=kind, why=why, subject=subject,
                             source="observed")
    except ValueError as exc:
        return _err(str(exc))
    replaced = [store.get(i) for i in before
                if i != mem.id and not store.get(i).active]
    _sync_memory_note(store)
    out = f"Remembered #{mem.id} ({mem.kind}): {mem.text}"
    if replaced:
        out += "\nSuperseded: " + "; ".join(f"#{m.id} {m.text}" for m in replaced)
    return out


@mcp.tool(
    description=(
        "Reword a memory you already hold, keeping its id, strength and "
        "history. Use this when the belief is right but you wrote it badly — "
        "too specific, too vague, or phrased as a request. To replace a belief "
        "with a different one, use vault_memory_write instead; to drop it, "
        "vault_memory_forget."
    )
)
def vault_memory_revise(memory_id: int, text: str = "", why: str = "") -> str:
    store = E().memory
    m = store.revise(memory_id, text=text, why=why)
    if not m:
        return _err(f"no memory #{memory_id}, or nothing to change")
    _sync_memory_note(store)
    return f"Revised #{m.id}: {m.text}"


@mcp.tool(
    description=(
        "Drop a memory that is wrong or was never worth keeping. It stays in "
        "the record as something you no longer believe — that history is how "
        "odd behaviour gets explained later — but it stops being injected. "
        "Give a reason."
    )
)
def vault_memory_forget(memory_id: int, reason: str = "") -> str:
    store = E().memory
    m = store.get(memory_id)
    if not m:
        return _err(f"no memory #{memory_id}")
    store.forget(memory_id, reason=reason or "no longer believed")
    _sync_memory_note(store)
    return f"Forgotten #{memory_id}: {m.text}"


# --- voice: the half of the character that grows ----------------------------
# Same store as memory, different section of the prompt. A trait is a lesson
# about how you *sound*; a preference is a lesson about how you *work*. The
# line between them is where the lesson lands: "answer in one line unless I ask
# for detail" governs behaviour and is a preference; "the 'sir' reads as padding
# in the morning brief" governs wording and is a trait. When genuinely unsure,
# write a preference — the voice block is capped at eight lines and the cap is
# there to keep the shipped character recognisable.


@mcp.tool(
    description=(
        "The habits of speech you have picked up from Parsa — the learned "
        "half of your character, on top of the fixed voice in your system "
        "prompt. Read before writing one so you revise rather than duplicate. "
        "Shows id, evidence, strength, and whether it is currently strong "
        "enough to be injected."
    )
)
def vault_voice_list(include_dropped: bool = False) -> str:
    store = E().traits
    if include_dropped:
        rows = store.all(active_only=False)
        if not rows:
            return "No voice traits learned yet."
        return "\n".join(m.describe() for m in rows)
    lines = store.describe()
    return "\n".join(lines) if lines else "No voice traits learned yet."


@mcp.tool(
    description=(
        "Learn a habit of speech from something Parsa said about how you "
        "sound. Use this ONLY for wording, tone, address and formatting of "
        "your replies — anything about what you should *do* is "
        "vault_memory_write with kind 'preference' instead.\n\n"
        "Write it as a durable instruction to yourself, in the imperative, "
        "with the situation it applies to: 'Skip the honorific in the morning "
        "brief' — not 'Parsa did not like that reply'. `why` is required and "
        "is the evidence: what he actually said or did. A trait you cannot "
        "attribute to something he said is one you invented, and this call "
        "refuses it.\n\n"
        "Do not write a trait that would soften bad news, add praise, or make "
        "you longer — those contradict rules you do not get to amend, and the "
        "block says so after your traits, so it will simply be overruled."
    )
)
def vault_voice_learn(text: str, why: str) -> str:
    store = E().traits
    before = {m.id for m in store.all()}
    try:
        t = store.learn(text, why, source="told")
    except ValueError as exc:
        return _err(str(exc))
    replaced = [store.memories.get(i) for i in before
                if i != t.id and not store.memories.get(i).active]
    _sync_memory_note(store.memories)
    out = f"Learned #{t.id}: {t.text}"
    if replaced:
        out += "\nSuperseded: " + "; ".join(f"#{m.id} {m.text}" for m in replaced)
    live = store.live()
    if t.id not in {m.id for m in live}:
        out += (f"\nNote: {len(live)} traits already fill the block, so this "
                "one is stored but not yet injected — it will be once it is "
                "reinforced past a weaker one.")
    return out


@mcp.tool(
    description=(
        "Drop a habit of speech that is wrong, stale, or that Parsa has just "
        "contradicted. It stays in the record as something you used to do. "
        "Tell him you dropped it rather than going quiet."
    )
)
def vault_voice_forget(trait_id: int, reason: str = "") -> str:
    store = E().traits
    t = store.memories.get(trait_id)
    if not t or t.kind != memory_mod.VOICE:
        return _err(f"no voice trait #{trait_id}")
    store.forget(trait_id, reason=reason or "no longer how he wants it")
    _sync_memory_note(store.memories)
    return f"Dropped #{trait_id}: {t.text}"


# --- editing: EDITH only ----------------------------------------------------
# Everything above this line is append-or-create. These three are the only
# tools in the server that can change text a human already wrote, and they are
# gated twice: `shells.py` leaves them out of the allowlist for every agent but
# EDITH, so they are normally not even visible, and each one re-checks below in
# case the server was launched directly. Two gates because the first one is the
# real protection and the second one is what catches the day somebody changes
# the first.


def _may_edit() -> str:
    """"" when the running agent may modify notes here, else why not.

    Two facts, not one: *who* is asking and *where from*. V edits at the
    terminal and in the HUD and is append-only on Telegram and the phone, so a
    gate that only knew the agent would read "this is V" and wave through a
    rewrite requested by a chat message.
    """
    named = os.environ.get("V_AGENT", "").strip()
    agent = persona_mod.get(named)
    where = os.environ.get("V_SURFACE", "").strip()
    # An unset `V_AGENT` means the server was started by something that did not
    # say who was asking — `.mcp.json` launching it for a bare editor session,
    # say. `persona.get("")` answers V, which is the right default for reading
    # and the wrong one for editing, so an unnamed caller edits nothing.
    if named and agent.may_edit(where):
        return ""
    if agent.edits and where:
        allowed = ", ".join(agent.edit_surfaces) or "nowhere"
        return _err(
            f"{agent.key} is append-only on {where}. Modifying a note that "
            f"already exists is allowed from {allowed} only. Append with "
            "vault_append, or ask Parsa to do it from the terminal.")
    return _err(
        f"{agent.key} is append-only. Modifying a note that already exists is "
        "EDITH's alone, and EDITH answers only at the terminal. Append to the "
        "note with vault_append, or tell Parsa what needs changing and let him "
        "summon her.")


@mcp.tool(
    description=(
        "Replace one exact run of text inside an existing note. EDITH ONLY — "
        "every other agent is append-only and will not see this tool.\n\n"
        "`old` must appear EXACTLY ONCE in the note: the call is refused if it "
        "is missing (you are working from a version of the note that does not "
        "exist — read it again) or if it appears more than once (include more "
        "surrounding lines until it is unique). Read the note with vault_read "
        "immediately before editing it; do not edit from memory of an earlier "
        "turn.\n\n"
        "Preserves everything you did not name. Commits with a [bot] prefix "
        "and returns the sha for vault_undo. Prefer several small edits over "
        "one vault_rewrite."
    )
)
def vault_edit(rel: str, old: str, new: str, summary: str | None = None) -> str:
    denied = _may_edit()
    if denied:
        return denied
    try:
        _privacy.check(rel)
        change = _vault.edit(rel, old, new, summary=summary)
    except (Blocked, VaultError) as exc:
        return _err(str(exc))
    return (f"edited {rel}\ncommit: {change.commit or '(git off)'}"
            f"\nundo with: vault_undo(commit='{change.commit}')")


@mcp.tool(
    description=(
        "Update frontmatter fields on an existing note, leaving the body "
        "untouched. EDITH ONLY. `updates` is a JSON object of field -> value; "
        "fields you do not name are kept. Use this rather than vault_edit for "
        "status changes, closed dates, ratings — anything a dashboard queries, "
        "because it cannot damage the body while changing a field."
    )
)
def vault_set_meta(rel: str, updates: str) -> str:
    denied = _may_edit()
    if denied:
        return denied
    try:
        _privacy.check(rel)
        meta = json.loads(updates)
    except Blocked as exc:
        return _err(str(exc))
    except json.JSONDecodeError as exc:
        return _err(f"updates is not valid JSON: {exc}")
    if not isinstance(meta, dict) or not meta:
        return _err("updates must be a non-empty JSON object")
    try:
        change = _vault.set_meta(rel, meta)
    except (Blocked, VaultError) as exc:
        return _err(str(exc))
    return (f"updated {', '.join(meta)} on {rel}"
            f"\ncommit: {change.commit or '(git off)'}"
            f"\nundo with: vault_undo(commit='{change.commit}')")


@mcp.tool(
    description=(
        "Replace a note's ENTIRE contents. EDITH ONLY, and the last resort — "
        "reach for vault_edit first, and for several vault_edit calls before "
        "this one.\n\n"
        "Everything you do not carry over is gone: a paragraph you forgot to "
        "reproduce leaves a note that still parses and still queries cleanly, "
        "so nothing looks wrong until Parsa goes looking for what is missing. "
        "Only use it when the note is genuinely being restructured, read it in "
        "full immediately beforehand, and say in your reply what you dropped."
    )
)
def vault_rewrite(rel: str, content: str, summary: str | None = None) -> str:
    denied = _may_edit()
    if denied:
        return denied
    try:
        _privacy.check(rel)
        if not _vault.exists(rel):
            return _err(f"{rel} does not exist — use vault_create")
        change = _vault.rewrite(rel, content, allow_rewrite=True,
                                summary=summary)
    except (Blocked, VaultError) as exc:
        return _err(str(exc))
    return (f"rewrote {rel}\ncommit: {change.commit or '(git off)'}"
            f"\nundo with: vault_undo(commit='{change.commit}')")


# --- Quera ------------------------------------------------------------------
# University deadlines were the one capability that existed everywhere except
# here: `cli.py` has `v quera sync`, `scheduler.py` runs it at 06:45 nightly,
# and the assignment notes it writes are queryable through `vault_query`. What
# was missing was any way for the *agent* to find that out, so every question
# about whether the deadlines were current was answered by guessing — and it
# guessed wrong in both directions on record, once inventing "no Quera access
# is set up for me" while the notes it was about to be asked for had been
# synced five days earlier, and once reporting the sync unverifiable because
# Quera is not listed in `Authenticated Sites`. It isn't listed there because
# it does not need a browser: it has a saved cookie and a module of its own.
#
# Split in two rather than given an `action` parameter, because the halves have
# different postures: status reads Quera and reports the diff, and belongs on
# every run; sync creates notes, and belongs behind the write flag with the
# other mutations.


def _quera():
    from ..core import quera as quera_mod
    return quera_mod.Quera(S, _vault)


def _quera_lines(q, deadlines) -> list[str]:
    """The live list, each line said against what the vault already holds.

    `q._assignment_note` rather than a path built here on purpose: the note's
    filename is the sync's own identity rule, and a second copy of it in this
    file is a second copy that drifts.
    """
    from ..core.quera import _s

    out = []
    for a in deadlines:
        rel, _, _ = q._assignment_note(a)
        due = a.due_at.date().isoformat() if a.due_at else "no date"
        if not _vault.exists(rel):
            state = "NOT IN THE VAULT"
        else:
            meta = _vault.read(rel).meta
            have, status = _s(meta.get("due-date")), _s(meta.get("status")) or "?"
            state = (f"in the vault · status {status}"
                     + ("" if have == due else f" · due-date says {have}"))
        out.append(f"- {a.course} — {a.title} · due {due} · {state}")
    return out


@mcp.tool(
    description=(
        "Quera deadline sync: whether it is configured, when the nightly job "
        "last ran, what Quera lists right now, and whether the vault matches. "
        "Reads only — it never writes a note.\n\n"
        "Call this before saying ANYTHING about whether Parsa's university "
        "deadlines are up to date. The sync runs on its own at 06:45 daily, so "
        "the default assumption is that they ARE current; never tell him you "
        "have no Quera access without calling this first and reading what it "
        "says. Assignment notes themselves are read with "
        "vault_query(kind='assignments').\n\n"
        "Note what Quera can and cannot tell you: the payload behind this is "
        "the site's own deadline widget, which carries only deadlines still "
        "open. An assignment already submitted, or one whose deadline has "
        "passed, is simply absent — so 'not in this list' means 'not "
        "outstanding on Quera', never 'does not exist'. Those have to be added "
        "by hand."
    )
)
def vault_quera_status() -> str:
    from ..core import scheduler as scheduler_mod
    from ..core import quera as quera_mod

    q = _quera()
    last = scheduler_mod.last_run(scheduler_mod.QUERA_JOB, S)
    head = [
        f"cookie configured: {'yes' if q.configured() else 'no'}",
        f"nightly job (06:45): "
        + (last.strftime("last ran %Y-%m-%d %H:%M") if last else "no record"),
    ]
    if not q.configured():
        return "\n".join(head + [
            "", "No saved session, so nothing has been pulled. Parsa "
            "re-authorises it himself with `v quera auth --cookie '...'` — "
            "you cannot do it for him, and there is no browser login here."])
    try:
        deadlines = q.fetch_deadlines()
    except quera_mod.NotAuthorised as exc:
        return "\n".join(head + ["", _err(
            f"{exc}\nOnly Parsa can fix this: `v quera auth --cookie '...'`.")])
    except quera_mod.QueraError as exc:
        return "\n".join(head + ["", _err(str(exc))])

    if not deadlines:
        return "\n".join(head + [
            "", "Quera lists no open deadlines at all. That is a real "
            "outcome — everything submitted or past — not a failure."])
    body = _quera_lines(q, deadlines)
    drift = [l for l in body if "NOT IN THE VAULT" in l or "due-date says" in l]
    tail = ("in sync — every open Quera deadline already has its note"
            if not drift else
            f"{len(drift)} of {len(body)} out of sync; vault_quera_sync writes them")
    return "\n".join(head + ["", f"Quera lists {len(deadlines)} open deadline(s):"]
                      + body + ["", tail])


@mcp.tool(
    description=(
        "Run the Quera deadline sync now: create the assignment notes Quera "
        "lists that the vault is missing, and correct a `due-date` that has "
        "moved. Never touches a note's body or its `status`, so work Parsa "
        "has already written down is safe.\n\n"
        "Call vault_quera_status first — usually it reports everything already "
        "in sync, because the nightly job at 06:45 has done it, and then this "
        "is not needed. Marking an assignment done is NOT this tool's job: "
        "that is a frontmatter change and only EDITH can make it."
    )
)
def vault_quera_sync() -> str:
    from ..core import quera as quera_mod

    q = _quera()
    if not q.configured():
        return _err("no Quera session saved — Parsa runs "
                    "`v quera auth --cookie '...'` himself.")
    try:
        report = q.sync(dry_run=False)
    except quera_mod.QueraError as exc:
        return _err(str(exc))
    if report.get("error"):
        return _err(report["error"])
    created, updated = report["created"], report["updated"]
    if not created and not updated:
        return (f"Already in sync — {report['skipped']} deadline(s) checked, "
                "nothing to write.")
    lines = []
    if created:
        lines += [f"created {len(created)}:"] + [f"- {r}" for r in created]
    if updated:
        lines += [f"updated {len(updated)}:"] + [
            f"- {u['rel']} — {u['changes']}" for u in updated]
    lines.append(f"({report['skipped']} already current)")
    return "\n".join(lines)


def main() -> None:
    """Console-script entry (`v-mcp`). stdio is the only transport an
    MCP client ever launches this over, so it is not a parameter."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
