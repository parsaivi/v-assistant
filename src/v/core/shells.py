"""Reasoning shells — the swappable half of the assistant.

`core/` answers everything it can with SQL and numpy, for nothing. What is left
over needs judgement, and judgement needs a model. This module is the seam
where one gets attached, and the seam is deliberately thin: a shell takes a
prompt and returns text plus what it cost. Nothing else in `core/` knows which
shell is running, or that there is one at all.

Three exist:

`NullShell`     the default. Refuses loudly. The system stays fully functional
                without a model — you just get retrieved evidence instead of a
                written answer.

`ClaudeCodeShell`  drives the Claude Code CLI headlessly. This is the one that
                works on a Claude Pro subscription: no API key, no per-token
                bill, drawing on the same plan allowance as the chat app. It
                inherits the vault's `.mcp.json` and `.claude/settings.json`,
                so the 12 vault tools and both halves of the privacy guard
                apply without being re-specified here.

`HermesShell`   drives Nous Research's Hermes Agent, if it is installed. Kept
                because it is the natural home for the Telegram/Discord
                gateways and its skills loop. **Its Anthropic OAuth path needs
                a Claude Max plan with purchased usage credits — Pro cannot use
                it** — so on a Pro account this shell is only reachable with a
                separate API key or a non-Anthropic provider.

A note on cost. On a subscription the marginal cost of a run is zero; what it
actually consumes is plan allowance, shared with your normal Claude usage. The
CLI still reports a dollar figure, computed locally at list rates. Calling that
"spend" would be a lie, so it is carried as `list_cost_usd` and rendered as an
equivalent, not a bill.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, settings as default_settings


class ShellUnavailable(RuntimeError):
    """Raised when a shell is asked to run and cannot."""


@dataclass
class ShellEvent:
    """Something that happened *during* a run, while it is still running.

    The design brief asks for a wait over four seconds to print the route step
    by step, and `ui.Spinner.step()` and the window's tool arms were both
    written for it and then fed nothing, because a run was one opaque
    `subprocess.run` that returned when it was over. These are what a surface
    can show while it waits.

    `kind` is one of:

      start        the run is up; `detail` is the session id
      think        the model is reasoning; `n` is its running token estimate
      text         a fragment of the answer as it is written
      tool         a tool call went out; `tool` is its name, `n` how many
                   have gone out so far
      tool_result  that call came back
      limit        the plan's rate-limit window changed state

    `detail` is a phrase for a person to read. `rel` and `rels` are the same
    information for a surface to *draw*: the note a call is about, and the
    notes a call came back holding. They are kept apart from `detail` because
    the moment a path is folded into a display string it has been truncated to
    sixty columns and mixed in with query text, and a note's identity cannot be
    got back out of that.

    Both are *candidates*, not verified paths — this module knows the shape of
    a vault note and deliberately nothing else. Resolving one to a note that
    exists belongs to whoever has the index open.
    """

    kind: str
    text: str = ""
    tool: str = ""
    detail: str = ""
    n: int = 0
    rel: str = ""
    rels: list[str] = field(default_factory=list)


# A callback taking one event. Never allowed to break a run: `_emit` swallows
# whatever it raises, because a surface with a bug in its progress bar must not
# be able to lose an answer that has already been paid for.
Progress = Callable[[ShellEvent], None] | None


# --- how heavy is this question? -------------------------------------------
# Deterministic, instant, free — and it runs before a single token is spent.
# Four tiers, because the questions really do come in four sizes:
#
#   trivial one sentence back, no reading: rephrase, confirm, tiny extraction
#   light   one fact, one lookup, a short answer
#   normal  the default: pull a few notes together and answer
#   heavy   audits, comparisons, "why did this change", multi-hop reading
#
TRIVIAL, LIGHT, NORMAL, HEAVY = "trivial", "light", "normal", "heavy"

# List rates, USD per million tokens (input, output), for the local cost
# estimate. Haiku 4.5 is the cheapest model available and the floor of the
# ladder — there is nothing below it, so `trivial` and `light` share it and
# differ only in effort. Kept here rather than in llm.py because this is the
# path that actually spends: llm.py's provider is inert by default.
MODEL_PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),      # cheapest available
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}
# What the CLI's short names resolve to, so a price lookup and a `--model haiku`
# refer to the same thing.
MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get((name or "").strip().lower(), name)


def price_of(model: str) -> tuple[float, float]:
    return MODEL_PRICES.get(resolve_model(model), (0.0, 0.0))

_HEAVY_HINTS = (
    "analy", "audit", "compare", "contrast", "trace", "investigate",
    "deep dive", "comprehensive", "across all", "across my", "every note",
    "whole vault", "all my", "everything", "review the", "why did", "why does",
    "how did", "evolve", "evolution", "restructure", "refactor", "migrate",
    "plan for", "strategy", "root cause", "inconsistenc", "reconcile",
    "بررسی", "تحلیل", "مقایسه",
)
_LIGHT_HINTS = (
    "when did", "when is", "when's", "who is", "who's", "what is", "what's",
    "how many", "how much", "where is", "where's", "which", "did i", "do i",
    "is there", "are there", "last time", "next",
)
# Matched as whole words rather than as a bare prefix. `"do it".startswith(
# "do i")` is true, and that is not hypothetical: on 2026-09-05 "do it" — the
# shortest possible "carry on with what you were doing" — was tiered `light`,
# handed to haiku with a 90-second ceiling, and timed out having produced
# nothing, in the middle of writing an internship report. A trailing `\b`
# costs nothing and makes the tuple mean what it reads as.
_LIGHT_OPENER = re.compile(
    r"^(?:" + "|".join(re.escape(h) for h in _LIGHT_HINTS) + r")\b")
# A question that wants prose rather than a fact is at least `normal`, however
# short it is: "summarise my week" is six words and not a lookup. So is
# anything asking *why* — a reason has to be assembled, not looked up.
_PROSE_HINTS = ("summar", "explain", "draft", "write", "suggest", "recommend",
                "should i", "help me", "why", "reason", "rationale",
                "خلاصه", "توضیح", "چرا")
# Work with no reading in it: reshape this text, confirm this, pull this field
# out. The vault isn't involved, so the cheapest model is not a compromise —
# it's the right tool, and anything above it is pure waste.
_TRIVIAL_HINTS = (
    "rephrase", "reword", "rewrite this", "shorten", "make it shorter",
    "tidy up", "fix the grammar", "spell", "translate", "capitalise",
    "capitalize", "title case", "bullet these", "list these", "format this",
    "tl;dr of this", "in one line", "in one sentence", "yes or no",
)


def weigh(prompt: str) -> str:
    """Pick a tier from the text.

    Wrong guesses are cheap in one direction: under-tiering a hard question
    wastes a fast call, over-tiering an easy one wastes allowance that Parsa
    also needs for his own Claude usage. So each cheap tier requires positive
    evidence and anything ambiguous lands on `normal`.
    """
    low = (prompt or "").lower().strip()
    words = low.split()

    if any(h in low for h in _HEAVY_HINTS) or len(words) > 45:
        return HEAVY
    # Trivial is checked before prose, because "rewrite this in one sentence"
    # matches both and the cheap reading is the correct one.
    if any(h in low for h in _TRIVIAL_HINTS) and len(words) <= 30:
        return TRIVIAL
    if any(h in low for h in _PROSE_HINTS):
        return NORMAL
    # Light needs a lookup *opener*, not just brevity. Judging on length alone
    # sent "what did I decide about pricing and why?" — eight words, and pure
    # synthesis — to haiku.
    if len(words) <= 14 and _LIGHT_OPENER.match(low):
        return LIGHT
    return NORMAL


TIERS = (TRIVIAL, LIGHT, NORMAL, HEAVY)


def heavier(tier: str, floor: str | None) -> str:
    """The heavier of two tiers; unknown floors are ignored.

    `weigh` reads one message, and a follow-up carries none of the weight of
    what it follows: "do it", "yes", "go on" are two words each and mean
    whatever the previous turn was in the middle of. Weighed alone they are
    cheap; weighed as the continuation of "write my internship report" they
    are not. So a resumed thread carries its tier forward as a floor — the
    follow-up may escalate it, never discount it.
    """
    order = {t: i for i, t in enumerate(TIERS)}
    if floor not in order:
        return tier
    return tier if order.get(tier, 0) >= order[floor] else floor


def tier_settings(tier: str, s: Settings) -> tuple[str, str]:
    """(model, effort) for a tier."""
    return {
        TRIVIAL: (s.tier_trivial_model, s.tier_trivial_effort),
        LIGHT: (s.tier_light_model, s.tier_light_effort),
        HEAVY: (s.tier_heavy_model, s.tier_heavy_effort),
    }.get(tier, (s.tier_normal_model, s.tier_normal_effort))


def idle_for(tier: str, s: Settings) -> int:
    """How long this tier may go silent before it counts as stuck.

    An idle budget, not a ceiling on the run. The distinction is the whole
    point: the thing worth killing is a run that has stopped doing anything,
    and total runtime cannot see that — it kills the long job and the hung one
    with equal confidence. Under the streaming reader, silence can be measured
    directly, so it is.

    Tiered anyway, because the longest legitimate gap between events does scale
    a little with the work: a lookup that says nothing for a minute is stuck,
    whereas an audit may sit inside one slow tool call for longer than that.
    """
    return {
        TRIVIAL: s.shell_idle_trivial,
        LIGHT: s.shell_idle_light,
        NORMAL: s.shell_idle_normal,
        HEAVY: s.shell_idle_heavy,
    }.get(tier) or s.shell_idle


@dataclass
class ShellResult:
    text: str
    provider: str
    model: str = ""
    session_id: str = ""
    turns: int = 0
    ms: float = 0.0
    list_cost_usd: float = 0.0     # list-rate equivalent, NOT a subscription bill
    billing: str = "subscription"  # subscription | api-key
    tier: str = ""                 # light | normal | heavy
    denials: list = field(default_factory=list)
    error: str | None = None
    raw: dict = field(default_factory=dict)
    # Which tools the run actually reached for. A run that called none of them
    # answered from its own context, which is the signal that it did not need
    # the tier it was given.
    tools_used: list = field(default_factory=list)
    # Token accounting, and specifically the two cache numbers. They were
    # sitting in the CLI's own output and being thrown away, which made the
    # prompt cache the one part of the cost model nobody could see: a prefix
    # that never caches and a prefix that always caches produce the same
    # `total_cost_usd` on a subscription, where cost is reported as an
    # equivalent rather than a bill.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # The plan's rate-limit window as the CLI last reported it: status,
    # `resetsAt` (epoch seconds), and which window. Read from the stream, so V
    # knows how close it is *before* the run that fails.
    rate_limit: dict = field(default_factory=dict)
    # True when `text` is what had been written when the run was cut off —
    # half an answer, kept, rather than nothing.
    partial: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def cached_fraction(self) -> float:
        """How much of the prefix came out of cache. The number that says
        whether the layout in `run()` is doing its job."""
        total = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return (self.cache_read_tokens / total) if total else 0.0

    def cost_note(self) -> str:
        if self.billing == "api-key":
            return f"${self.list_cost_usd:.4f}"
        return f"≈${self.list_cost_usd:.4f} of plan allowance"


class Shell(ABC):
    name = "shell"

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def run(self, prompt: str, *, session_id: str | None = None,
            allow_writes: bool = False, timeout: int | None = None,
            model: str | None = None, effort: str | None = None,
            memory: str = "", agent: str = "v", tier_floor: str | None = None,
            surface: str = "", on_event: Progress = None) -> ShellResult: ...


# ---------------------------------------------------------------------------
class NullShell(Shell):
    """The zero-cost default. Refuses in a way that says what to do about it."""

    name = "null"

    @property
    def available(self) -> bool:
        return False

    def run(self, prompt: str, **_) -> ShellResult:  # noqa: D102
        raise ShellUnavailable(
            "No reasoning shell is configured, so I can retrieve but not "
            "reason. Turn one on with:  python3 -m v.cli shell --use "
            "claude-code"
        )


# ---------------------------------------------------------------------------
from . import persona
from .identity import SYSTEM  # noqa: E402  — V's one description of itself

# A curated slice of Claude Code's Chrome-extension tools (there are ~40; see
# claude-in-chrome's own tool list), for sites that need Parsa's logged-in
# session — his university portal first, more to come via
# `19 - Assistant/Authenticated Sites.md`. Deliberately narrow: navigate,
# read, and click/type, not JS eval or screenshots.
#
# Worth being honest about, same as `HermesShell` below: this depends on the
# Chrome extension being attached to *this* process's Claude Code session.
# That holds when V is invoked from an interactive terminal with the
# extension paired to it, and may simply not connect at all for a detached
# or cron invocation — a caveat, not a guarantee.
BROWSER_TOOLS = [
    "mcp__claude-in-chrome__tabs_context_mcp", "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__tabs_create_mcp", "mcp__claude-in-chrome__tabs_close_mcp",
    "mcp__claude-in-chrome__computer", "mcp__claude-in-chrome__read_page",
    "mcp__claude-in-chrome__find", "mcp__claude-in-chrome__get_page_text",
]

# These four lists are the shell's whole surface: a tool absent from all of
# them is a tool the agent cannot call, however well the system prompt
# describes it.
# `test_shell` asserts every tool registered on the MCP server appears in one of
# them, because the failure is silent — asked to update goals and file a todo
# list, the agent reached for `Write`, got denied, and reported that it had no
# write access, while `vault_add_task` sat unlisted.
READ_TOOLS = [
    "mcp__vault__vault_query", "mcp__vault__vault_search",
    "mcp__vault__vault_read", "mcp__vault__vault_nudges",
    "mcp__vault__vault_validate", "mcp__vault__vault_status",
    "mcp__vault__vault_standup", "mcp__vault__vault_reminders",
    "mcp__vault__vault_tasks", "mcp__vault__vault_calendar",
    # The week's shape, and the gaps in it. Read-only and free — and the only
    # tools that can answer "when am I free", which the calendar cannot.
    "mcp__vault__vault_schedule_week", "mcp__vault__vault_free_time",
    "mcp__vault__vault_now",
    "mcp__vault__vault_memory_list", "mcp__vault__vault_voice_list",
    # Emails and phone numbers out of `10 - Personal CRM`, and nothing else
    # from it. On the read list rather than the write one because it changes
    # nothing; the folder is otherwise private and stays that way.
    "mcp__vault__vault_contact",
    # The live Google account. Reading it is safe in the same way reading the
    # vault is — nothing leaves and nothing changes — so it belongs on every
    # run, not behind the write flag. Without it here, "what's on Arash's
    # calendar" has no tool to reach for and comes back as "I have no access",
    # which is the exact failure this list exists to prevent.
    "mcp__vault__gcal_events", "mcp__vault__gcal_calendars",
    # Gmail: search, read and draft are all read-only in effect — a draft
    # sits unsent until gmail_send is explicitly called, so creating one is
    # no more consequential than reading a message. Only gmail_send is a
    # write. Not reached through claude.ai's own generic Gmail connector,
    # for the same reason gcal isn't — see `ClaudeCodeShell`'s
    # `--strict-mcp-config` below.
    "mcp__vault__gmail_search", "mcp__vault__gmail_read",
    "mcp__vault__gmail_draft",
    # Charts/maps/trends: read-only, each one builds and opens its own HTML
    # file rather than returning anything the model has to reformat — see
    # `mcp/server.py`'s module docstring and `core/render.py`.
    "mcp__vault__vault_chart", "mcp__vault__vault_trend", "mcp__vault__vault_map",
    # Directions between two places, plus the drawn route. Read-only in the
    # same way the charts are — it opens a page and returns a sentence. Here
    # rather than behind the write flag because the failure this prevents is
    # the one on record: asked how long Divar → Tajrish takes with no routing
    # tool in reach, V estimated "20-35 minutes" and correctly labelled it a
    # guess. A guess was the best answer available; it shouldn't have been.
    "mcp__vault__vault_route",
    # Live POI search (Neshan), for places not yet in the vault. Read-only —
    # nothing is saved without going through vault_create separately.
    "mcp__vault__vault_places_search",
    # The two lookups either side of a coordinate. `vault_address` is the
    # only thing V can reach that knows whether somewhere is inside طرح
    # ترافیک or زوج/فرد — Google models neither — so without it in reach,
    # "can I drive to Omlet Sara today" gets answered from nothing, which is
    # the same failure `vault_route` was added for. `vault_geocode` is the
    # other direction and is here for one job: getting the `location` field
    # for a new place note. Both read-only; neither writes anything.
    "mcp__vault__vault_address", "mcp__vault__vault_geocode",
    # Whether the university deadlines are current. Read-only: it fetches
    # Quera's open-deadline list and diffs it against the assignment notes,
    # writing nothing. On every run rather than behind the write flag for the
    # same reason `gcal_events` is — without a tool to reach for, "are my
    # deadlines synced?" gets answered by guessing, and the two guesses on
    # record were "no Quera access is set up for me" and "I cannot verify the
    # live Quera sync", both said about a sync that had run that morning.
    "mcp__vault__vault_quera_status",
    # The Persian calendar. Read-only, and here rather than left to the model
    # because a Jalali date computed from memory is wrong at every leap year
    # and looks exactly as confident as a right one. It is also the only
    # thing in reach that knows whether tomorrow is تعطیل, which is not
    # derivable from anything local except a Friday.
    "mcp__vault__vault_jalali",
    # What things cost, new and used, in toman. Read-only — both are
    # searches, neither writes anything, and a price is never filed as a
    # ledger line without Parsa saying so. Here for the same reason
    # `vault_route` is: without them, "is 3 million reasonable for this" is
    # answered from a model's recollection of Iranian prices, which is a
    # guess wearing the authority of a lookup, in a currency that moves
    # every week.
    "mcp__vault__vault_digikala", "mcp__vault__vault_divar",
    "Read", "Grep", "Glob",
] + BROWSER_TOOLS
WRITE_TOOLS = [
    "mcp__vault__vault_append", "mcp__vault__vault_create",
    "mcp__vault__vault_remind", "mcp__vault__vault_undo",
    "mcp__vault__vault_task_done", "mcp__vault__vault_add_task",
    "mcp__vault__vault_schedule", "mcp__vault__vault_add_block",
    # Changing a block that already exists. These *are* edits, and they are
    # here rather than in `EDIT_TOOLS` on purpose: they take a date and a
    # time, can only rewrite one line of one `## Schedule` section, check the
    # kind against a closed vocabulary first, and refuse a line they cannot
    # find exactly once. "My class was cancelled" is the most obvious thing
    # to say to V from a phone, and gating it to the terminal would make the
    # schedule the one thing V can add to but never correct.
    "mcp__vault__vault_remove_block", "mcp__vault__vault_move_block",
    # Creating a Google event is the one write here that leaves the machine:
    # it emails the guests, and `vault_undo` does not reach it. Behind the
    # write flag like everything else, and the tool description says to
    # confirm the details first.
    "mcp__vault__gcal_add_event",
    # Sending a Gmail draft is the most irreversible write in this list —
    # unlike a vault note or even a calendar event, there is no undo at all.
    # The gate that matters is the one in gmail_draft's/gmail_send's tool
    # descriptions and identity.py's system prompt: never call this in the
    # same turn a draft was created. Being on WRITE_TOOLS is a second,
    # structural gate underneath that — it's simply absent on a read-only run.
    "mcp__vault__gmail_send",
    # Memory is a write path like any other: append-and-supersede, with the
    # old belief kept. Reading it (`vault_memory_list`) is on every run, since
    # knowing what you already believe is what stops you storing it twice.
    "mcp__vault__vault_memory_write", "mcp__vault__vault_memory_revise",
    "mcp__vault__vault_memory_forget",
    # The voice traits are the same store and the same argument, one section
    # of the prompt over. `core/traits.py` caps them and renders the rules
    # they cannot override after them, so the worst a bad write does is spend
    # one of eight slots until Parsa drops it.
    "mcp__vault__vault_voice_learn", "mcp__vault__vault_voice_forget",
    # Running the deadline sync on demand. It creates assignment notes, so it
    # is a write like any other — append-only, committed, undoable. Rarely
    # needed: the 06:45 job usually got there first, which is what
    # `vault_quera_status` is for saying.
    "mcp__vault__vault_quera_sync",
    # Pushing a vault file out to Telegram. It writes nothing, but it is the
    # only tool here that puts note content on someone else's server, and
    # there is no recalling a sent message — so it sits behind the write flag
    # with the other one-way doors rather than on every read-only run.
    "mcp__vault__vault_send_file",
]

# The tools that can change text a human already wrote. Kept out of
# `WRITE_TOOLS` on purpose: every other agent is append-only *by construction*
# rather than by instruction — these names never enter its allowlist, so no
# prompt and no misread instruction can produce a modification. Added only for
# an agent whose `Persona.may_edit(surface)` is true: EDITH, who only ever runs
# at the terminal, and V when the words arrived at the terminal or the HUD.
# Telegram and the phone never see these names, whoever is asking.
EDIT_TOOLS = [
    "mcp__vault__vault_edit",       # one exact, unique string
    "mcp__vault__vault_set_meta",   # frontmatter only, body untouched
    "mcp__vault__vault_rewrite",    # the whole note; last resort
]

# Claude Code's own tools, granted on every run alongside the vault ones.
#
# These exist because half of what Parsa asks for lives outside the vault and
# has to be fetched before it can be written down. `CLAUDE.md` documents the
# work workflow as "run `glab issue list`, then turn the JSON into notes" — V
# was reading the first half of that instruction and then asking Parsa to run
# the command himself, which is the assistant doing the easy half.
#
# `Bash` is the one that changes the threat model, and it is worth being blunt
# about: the deny rules in `.claude/settings.json` bind Read/Grep/Glob/Write/
# Edit, and a shell binds none of them. `cat "12 - Brain Dump/x.md"` is not a
# Read, and `python3 -c "open(...).write(...)"` is not a Write, so neither the
# privacy half nor the append-only half of the guard survives contact with a
# subprocess. What holds instead is the system prompt (see `identity.SYSTEM`)
# and the fact that every run is logged. That is a real downgrade from an
# enforced guarantee to an instructed one; it buys V the ability to actually do
# the work, and the trade was made deliberately rather than by omission.
HOST_TOOLS = [
    "Bash", "BashOutput", "KillShell",
    "WebSearch", "WebFetch",
    "TodoWrite", "Task",
]
# The write half of the host toolset. Still fenced by the `Write`/`Edit` deny
# rules on `NN - Folder/**/*.md`, so these reach the scripts and configs at the
# vault root but not a single note — vault prose stays on the vaultfs path.
HOST_WRITE_TOOLS = ["Write", "Edit", "NotebookEdit"]

# Where those deny rules now live. They sat in the vault's
# `.claude/settings.json` until 2026-09-13, which meant every Claude Code
# session opened in the vault inherited them — Parsa's own interactive ones
# included, where they were never the point. `ClaudeCodeShell` passes this
# file with `--settings` so the fence binds V and nothing else; the privacy
# half stays in the vault settings, because *that* half should bind everyone.
# `tests/test_shell.py` and `tests/test_mcp.py` assert this file still covers
# every granted file writer and every numbered folder on disk.
GUARD_SETTINGS = Path(__file__).with_name("guard.settings.json")


def _emit(on_event: Progress, event: ShellEvent) -> None:
    """Hand an event to the surface, and never let it cost an answer."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:                    # a broken progress bar is not an error
        pass


@dataclass
class _Live:
    """What is known about a run while it is still running.

    Everything here is also in the final payload, *if* there is one. The point
    is the case where there is not: a kill at the deadline leaves this object
    holding the session id, the tools that were called and the text written so
    far, which is what makes a timeout survivable.
    """

    text: str = ""
    session_id: str = ""
    turns: int = 0
    tools: list = field(default_factory=list)
    rate_limit: dict = field(default_factory=dict)
    _open_text: bool = False

    def absorb(self, event: dict, emit) -> None:
        kind = event.get("type")

        if kind == "system":
            sub = event.get("subtype")
            if sub == "init":
                self.session_id = event.get("session_id", "") or self.session_id
                emit(ShellEvent("start", detail=self.session_id,
                                n=len(event.get("tools") or [])))
            elif sub == "thinking_tokens":
                emit(ShellEvent("think", n=int(event.get("estimated_tokens") or 0)))
            return

        if kind == "stream_event":
            inner = event.get("event") or {}
            itype = inner.get("type")
            if itype == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "text":
                    # A second block of prose is a new paragraph, not a
                    # continuation — without this the salvaged text of a
                    # timed-out run runs its turns together into one sentence.
                    if self.text.strip():
                        self.text += "\n\n"
                    self._open_text = True
            elif itype == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    piece = delta.get("text") or ""
                    self.text += piece
                    emit(ShellEvent("text", text=piece))
            elif itype == "content_block_stop":
                self._open_text = False
            return

        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    self.tools.append(name)
                    emit(ShellEvent("tool", tool=name, n=len(self.tools),
                                    detail=_tool_detail(block.get("input")),
                                    rel=_input_rel(block.get("input"))))
            return

        if kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    emit(ShellEvent("tool_result", n=len(self.tools),
                                    tool=self.tools[-1] if self.tools else "",
                                    rels=_result_rels(block.get("content"))))
            return

        if kind == "rate_limit_event":
            self.rate_limit = event.get("rate_limit_info") or {}
            if self.rate_limit.get("status") not in ("allowed", None):
                emit(ShellEvent("limit", detail=str(self.rate_limit.get("status")),
                                n=int(self.rate_limit.get("resetsAt") or 0)))
            return

        if kind == "result":
            self.turns = int(event.get("num_turns") or 0)


def _tool_detail(payload) -> str:
    """One short phrase saying what a tool call is *about*.

    "vault_search" on its own is not progress — it is the same word every time.
    "vault_search · pricing refactor" is.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("query_text", "query", "question", "rel", "rel_path", "path",
                "file_path", "text", "command", "summary", "title", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:60]
    return ""


# What a vault note's path looks like, and the whole of what this module knows
# about the vault. Every folder is `NN - Name`, so a path is recognisable on
# sight without opening an index, without importing `config`, and — the point —
# without caring whether it arrived absolute (`Read(file_path=…)`), relative
# (`vault_read(rel=…)`), or embedded in a page of search results.
#
# Non-greedy to the first `.md`, and it stops at a quote, a backtick, a pipe or
# a newline: those are the characters that end a path inside JSON, inside a
# markdown table and inside a wikilink, and none of them can appear in one.
_VAULT_REL = re.compile(r"(\d{2} - [^\n\r\"'`|\]]*?\.md)")
# How many notes a single result may light up. A search returns eight, a query
# can return a hundred, and a hundred nodes arriving at once is not a constellation
# — it is the whole graph going white.
_MAX_RESULT_RELS = 12


def _input_rel(payload) -> str:
    """The note a tool call is about, if it is about one.

    A call names its note in the argument and not in the result — this is what
    makes "it is reading *this*" available at the moment the call goes out
    rather than after it comes back.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("rel", "rel_path", "path", "file_path", "note"):
        value = payload.get(key)
        if isinstance(value, str):
            found = _VAULT_REL.search(value)
            if found:
                return found.group(1)
    return ""


def _result_rels(content) -> list[str]:
    """The notes a tool call came back holding, in the order it listed them.

    This is the half that `vault_search` has and `vault_read` does not: one
    call names eight notes at once. A surface drawing the route gets a
    constellation out of it instead of a single point, which is a truer
    picture of what the search actually did.
    """
    if isinstance(content, list):
        text = "\n".join(b.get("text") or "" for b in content
                         if isinstance(b, dict))
    elif isinstance(content, str):
        text = content
    else:
        return []
    out: list[str] = []
    for match in _VAULT_REL.finditer(text):
        rel = match.group(1)
        if rel not in out:                      # a hit cited twice is one note
            out.append(rel)
        if len(out) >= _MAX_RESULT_RELS:
            break
    return out


class ClaudeCodeShell(Shell):
    """Claude Code, headless. Works on a Pro subscription — no API key."""

    name = "claude-code"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        self.bin = shutil.which("claude")

    @property
    def available(self) -> bool:
        return bool(self.bin)

    @property
    def billing(self) -> str:
        return "api-key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription"

    def run(self, prompt: str, *, session_id: str | None = None,
            allow_writes: bool = False, timeout: int | None = None,
            model: str | None = None, effort: str | None = None,
            memory: str = "", agent: str = "v", tier_floor: str | None = None,
            surface: str = "", on_event: Progress = None) -> ShellResult:
        if not self.available:
            raise ShellUnavailable(
                "the `claude` CLI is not on PATH — install Claude Code first")

        tier = heavier(weigh(prompt), tier_floor)
        default_model, default_effort = tier_settings(tier, self.s)
        model = model or default_model
        effort = effort or default_effort
        idle = timeout or idle_for(tier, self.s)

        # What is fixed goes in the system prompt; what changes per question
        # goes in the turn. That ordering is the whole point.
        #
        # Memory and the local search results used to be appended to `SYSTEM`,
        # which reads well — a preference Parsa taught sits after the defaults
        # V shipped with, so it overrides them — and cost more than it looked
        # like: both blocks are computed *from the question*, so the system
        # prompt was different on every run, and a prefix that changes below
        # some point cannot be cached below that point.
        #
        # In the turn instead, they are still ahead of the question and still
        # the most recent thing said, so the override still holds, and `SYSTEM`
        # is byte-identical from one run to the next. Measured on two runs with
        # different questions and different memory blocks (2026-08-22, haiku):
        #
        #   memory in the system prompt   54% cached · 14.1K re-read · $0.031
        #   memory in the turn            71% cached ·  8.7K re-read · $0.021
        #
        # Note what the first row is *not*: zero. The tool definitions sit
        # above the system prompt and cached either way — this buys the ~5K
        # tokens between them, on every escalation, not a cold prefix made
        # warm. `ShellResult.cached_fraction` is how you check it still holds
        # rather than assuming it.
        system = SYSTEM
        turn = f"{memory.strip()}\n\n---\n\n{prompt}" if memory.strip() else prompt
        tools = READ_TOOLS + HOST_TOOLS + (
            WRITE_TOOLS + HOST_WRITE_TOOLS if allow_writes else [])
        # Editing is a capability of the agent *on a surface*. A read-only run
        # gets none of it either way; a writing run gets it only if this agent
        # may modify existing notes **and** the words arrived somewhere it is
        # trusted to. `surface` defaults to empty, which no persona lists, so a
        # caller that forgets to pass it gets the append-only toolset rather
        # than the permissive one — the safe direction to fail in.
        if allow_writes and persona.get(agent).may_edit(surface):
            tools += EDIT_TOOLS
        sid = session_id or str(uuid.uuid4())
        cmd = [
            self.bin, "-p", turn,
            "--model", model,
            "--effort", effort,
            # dontAsk denies anything not explicitly allowed. There is nobody
            # at the keyboard to answer a permission prompt, and a headless
            # agent that blocks forever is worse than one that says no.
            "--permission-mode", "dontAsk",
            "--allowed-tools", *tools,
            "--append-system-prompt", system,
            "--setting-sources", "project,local",
            # The append-only half of the guard, and the reason it is passed
            # here rather than left in the vault's `.claude/settings.json`
            # beside the privacy half: that file is read by *every* Claude Code
            # session opened in the vault, Parsa's own included, and the rules
            # are about V, not about a human at the keyboard.
            #
            # It cannot be split by setting source. The line above loads
            # `project,local`; an interactive session loads `user,project,
            # local`. V's sources are a strict subset of his, so no source
            # exists that V reads and he does not. `--settings` is the seam
            # that does separate them — an additional file, highest
            # precedence, named only here.
            "--settings", str(GUARD_SETTINGS),
            # This account also has claude.ai's own generic Google
            # Calendar/Gmail/Drive connectors attached (`claude mcp list`),
            # unrelated to the vault and gated by their own OAuth. Without
            # this flag those show up in V's tool list right alongside
            # `mcp__vault__gcal_*`, and the model has picked the wrong one —
            # asked to book a meeting on Arash's calendar, it reported
            # needing to authorize "the connector" via claude.ai settings
            # instead of just calling `gcal_add_event`, which was already
            # configured and working. `--strict-mcp-config` limits the MCP
            # surface to exactly the servers named below, so `.mcp.json`'s
            # `vault` server is the only one V can ever see. `--chrome` has
            # to be repeated here because `--strict-mcp-config` otherwise
            # also drops the Chrome extension's own MCP server
            # (`claude-in-chrome`) along with the account connectors —
            # confirmed by hand: without it, BROWSER_TOOLS silently vanish
            # from the run even with the extension paired.
            "--strict-mcp-config",
            "--mcp-config", str(self.s.vault / ".mcp.json"),
            "--chrome",
            # The other half of the caching argument above, for the part of
            # the prompt V does not write: the CLI's own system prompt carries
            # the cwd, the environment, the git status and the memory paths,
            # all of which move on their own. This moves them into the first
            # user message, leaving a system prefix that is the same on this
            # machine tomorrow as it is today.
            "--exclude-dynamic-system-prompt-sections",
        ]
        # A ceiling on one run, enforced by the CLI rather than noticed in the
        # log afterwards. Be precise about what it does: it is checked at the
        # *turn boundary*, so it stops a run that keeps going — the $0.92 on
        # record was sixteen turns — and it cannot stop one expensive turn.
        # Confirmed by hand with a cap of $0.0001, which returned
        # `subtype: error_max_budget_usd` after spending $0.014.
        if self.s.shell_max_budget_usd:
            cmd += ["--max-budget-usd", str(self.s.shell_max_budget_usd)]
        # An overloaded model is a reason to answer more cheaply, not a reason
        # to fail the turn — the local-search fallback below is a worse answer
        # than the same question on Haiku.
        if self.s.shell_fallback_model and \
                resolve_model(self.s.shell_fallback_model) != resolve_model(model):
            cmd += ["--fallback-model", self.s.shell_fallback_model]
        # Resuming keeps the conversation; a fresh id starts one. Either way the
        # id comes back out so the caller can continue this thread later.
        cmd += ["--resume", session_id] if session_id else ["--session-id", sid]

        # Pin the OAuth profile explicitly — the machine's default
        # (~/.claude) has a dead token, and relying on whatever
        # CLAUDE_CONFIG_DIR the parent process happens to have is how that
        # bit us in the first place.
        # Which agent is asking. The MCP server is a *separate process* the
        # CLI launches from `.mcp.json`, so it cannot see `engine.persona` —
        # without this it would open its memory store at the default scope and
        # every tool call EDITH made would write into V's beliefs, quietly
        # undoing the isolation one level below where it is enforced.
        # …and where it is being asked from, for the same reason: the second
        # edit gate in `mcp/server.py` re-checks the persona in its own
        # process, and since V now edits on some surfaces and not others, that
        # check needs the surface or it degrades to "is this V?" — which is
        # true on Telegram too.
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.s.claude_config_dir,
                   V_AGENT=agent or "v", V_SURFACE=surface or "")

        streaming = self.s.shell_stream
        if streaming:
            # `--verbose` is not optional here: the CLI refuses stream-json
            # under `-p` without it. It does not make the run chattier — the
            # stream is the output format.
            cmd += ["--output-format", "stream-json",
                    "--include-partial-messages", "--verbose"]
        else:
            cmd += ["--output-format", "json"]

        t0 = time.time()
        read = self._stream if streaming else self._blob
        try:
            data, live, err = read(cmd, env, idle, on_event)
        except OSError as exc:
            return ShellResult("", self.name, ms=(time.time() - t0) * 1000,
                               billing=self.billing, tier=tier, model=model,
                               error=f"could not start the CLI: {exc}")
        ms = (time.time() - t0) * 1000

        if data is None:
            # No final payload — stalled, crashed, or wrote something that
            # was not JSON. `live` is whatever the run had said by then, and
            # keeping it is the difference between a wasted four minutes and a
            # half-finished answer: the kills on record were long, expensive
            # runs that had already done most of the work.
            return ShellResult(
                text=(live.text or "").strip()[:8000], provider=self.name,
                model=model, tier=tier, ms=ms, billing=self.billing,
                session_id=live.session_id or sid, turns=live.turns,
                tools_used=live.tools, rate_limit=live.rate_limit,
                partial=bool(live.text.strip()),
                error=err or f"stalled — no output for {idle}s")

        usage = data.get("modelUsage") or {}
        if usage:
            model = max(usage, key=lambda m: usage[m].get("outputTokens", 0))
        u = data.get("usage") or {}
        # A run can end in error and still have said something useful. The
        # budget ceiling is the clearest case: it fires *after* the turn that
        # crossed it, and reports `is_error` with an empty `result` — the
        # answer that turn wrote is only in the stream. Same shape as the
        # mid-stream stall in the log. So the text falls back to what was
        # streamed, and `partial` marks it as the fragment it is.
        said = (data.get("result") or "").strip()
        errored = bool(data.get("is_error"))
        error = None
        if errored:
            # `error_max_budget_usd` carries its meaning in the subtype and
            # leaves `result` empty; a plain "" here would reach the user as
            # "the agent couldn't run ()".
            error = said or str(data.get("subtype") or "the run failed")
        return ShellResult(
            text=(said or live.text or "").strip(),
            provider=self.name,
            model=model,
            tier=tier,
            session_id=data.get("session_id", sid),
            turns=data.get("num_turns", 0),
            ms=ms,
            list_cost_usd=float(data.get("total_cost_usd") or 0.0),
            billing=self.billing,
            denials=data.get("permission_denials") or [],
            error=error,
            partial=bool(errored and not said and live.text.strip()),
            tools_used=live.tools,
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
            rate_limit=live.rate_limit,
            raw=data,
        )

    # ---- reading the run ------------------------------------------------
    def _blob(self, cmd, env, idle, on_event):
        """One JSON object at the end. The old behaviour, kept whole.

        Nothing is known until the process exits, so a timeout here really does
        lose everything — which is why it is no longer the default. It takes
        `on_event` and ignores it for the same reason: a surface should not
        have to ask which reader is running to know whether to draw progress;
        it simply gets none here.

        It also cannot tell working from hung — that is what the stream is for
        — so the idle budget is spent here as a flat ceiling on the whole run.
        A blob reader that waits forever on a hung child has no way back.
        """
        del on_event
        try:
            proc = subprocess.run(
                cmd, cwd=self.s.vault, capture_output=True, text=True,
                timeout=idle, env=env, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return None, _Live(), f"timed out after {idle}s"
        if proc.returncode != 0 and not proc.stdout.strip():
            return None, _Live(), (proc.stderr or
                                   f"claude exited {proc.returncode}").strip()[:500]
        try:
            return json.loads(proc.stdout), _Live(), ""
        except json.JSONDecodeError:
            live = _Live()
            live.text = proc.stdout.strip()[:4000]
            return None, live, "could not parse the CLI's JSON output"

    def _stream(self, cmd, env, idle, on_event):
        """Read the run as it happens, and keep what it said if it is cut off.

        The deadline is against *silence*, not against the length of the run.
        A run that is stuck mid-tool emits nothing at all and one that is
        working emits constantly — tool calls, tool results, message deltas —
        so the gap between events is the signal, and the total is not: it
        cannot separate a job that legitimately takes eight minutes from a
        child that died holding the pipe open. Every event pushes the deadline
        out, which means a run is killed for being stuck and never merely for
        being long.

        A reader thread does the blocking read so the deadline stays
        enforceable — `readline()` on a hung child is uninterruptible, and a
        timeout you cannot enforce is a comment.
        """
        proc = subprocess.Popen(
            cmd, cwd=self.s.vault, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
            # Closed rather than inherited: the CLI waits three seconds for
            # input that is never coming, on every run. See `decide.py`.
            stdin=subprocess.DEVNULL, bufsize=1,
        )
        lines: queue.Queue = queue.Queue()

        def pump():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        # stderr needs a thread of its own, not a read at the end. A pipe
        # nobody drains fills at ~64KB and then blocks the writer — so a run
        # that printed a lot of warnings would hang until the deadline killed
        # it, with the stall looking exactly like a slow model.
        errors: list = []

        def drain():
            try:
                errors.append(proc.stderr.read() or "")
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        threading.Thread(target=drain, daemon=True).start()

        live = _Live()
        # Reset by every line the child produces, parseable or not: a stray log
        # line is not an answer, but it is proof the process is alive, which is
        # the only thing this deadline is asking about.
        last_seen = time.time()
        final, err = None, ""
        while True:
            left = (last_seen + idle) - time.time()
            if left <= 0:
                proc.kill()
                err = f"stalled — no output for {idle}s"
                break
            try:
                line = lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if line is None:                      # stdout closed: run is over
                break
            last_seen = time.time()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue                          # a stray log line is not fatal
            if event.get("type") == "result":
                final = event
            live.absorb(event, lambda e: _emit(on_event, e))

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if final is None and not err:
            stderr = "".join(errors).strip()
            err = stderr[:500] or f"claude exited {proc.returncode} with no result"
        return final, live, err


# ---------------------------------------------------------------------------
class HermesShell(Shell):
    """Nous Research's Hermes Agent, if installed.

    Untested against a live install — the binary is not present on this
    machine, so treat this as a wiring sketch rather than a verified path.
    Worth keeping because Hermes brings the messaging gateways (Telegram,
    Discord, Slack, WhatsApp) and a skills loop that would otherwise be built
    from scratch. Its Anthropic OAuth route requires a Claude Max plan with
    purchased usage credits; a Pro account must supply an API key or point it
    at another provider.
    """

    name = "hermes"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        self.bin = shutil.which("hermes")

    @property
    def available(self) -> bool:
        return bool(self.bin)

    def run(self, prompt: str, *, session_id: str | None = None,
            allow_writes: bool = False, timeout: int | None = None,
            model: str | None = None, effort: str | None = None,
            memory: str = "", agent: str = "v", tier_floor: str | None = None,
            surface: str = "", on_event: Progress = None) -> ShellResult:
        del memory, surface, on_event  # unverified sketch; see the docstring
        if not self.available:
            raise ShellUnavailable(
                "the `hermes` CLI is not installed. Install it with:\n"
                "  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash\n"
                "then `hermes setup --portal`. Note its Anthropic OAuth path "
                "needs Claude Max + usage credits; on Pro, configure an API "
                "key or another provider instead."
            )
        tier = heavier(weigh(prompt), tier_floor)
        model = model or self.s.hermes_model
        cmd = [self.bin, "chat", "--provider", self.s.hermes_provider,
               "--model", model, prompt]
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, cwd=self.s.vault, capture_output=True,
                                  text=True,
                                  timeout=timeout or self.s.shell_idle)
        except subprocess.TimeoutExpired:
            return ShellResult("", self.name, ms=(time.time() - t0) * 1000,
                               error="timed out")
        ms = (time.time() - t0) * 1000
        if proc.returncode != 0:
            return ShellResult("", self.name, ms=ms,
                               error=(proc.stderr or "hermes failed").strip()[:500])
        return ShellResult(proc.stdout.strip(), self.name, model=model,
                           tier=tier, ms=ms, billing="api-key")


# ---------------------------------------------------------------------------
SHELLS = {"null": NullShell, "claude-code": ClaudeCodeShell, "hermes": HermesShell}


def build(settings: Settings | None = None) -> Shell:
    s = settings or default_settings
    cls = SHELLS.get(s.shell_provider, NullShell)
    shell = cls(s) if cls is not NullShell else NullShell()
    # A configured-but-missing shell must say so rather than silently degrade.
    # Falling back quietly to NullShell looks exactly like the system working,
    # just worse — the same failure the embedding loader had.
    return shell
