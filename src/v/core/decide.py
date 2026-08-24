"""The model decides what a message *is*. Rules only handle what can't be wrong.

The router's regexes were the whole decision for a year, and the failure mode
they have is not "no match" — it's *confidently wrong*. "i should set a fixed
time on sundays for my 1on1 meetings with Arash" matched `CALENDAR_RE` on the
substring `1on1`, returned 85% confidence, and printed today's calendar. The
`i should` that makes it a commitment was scored twenty lines further down and
never got a vote. No confidence threshold catches that, because the rule was
certain and wrong; the only fix is to stop letting a substring be the decider.

So the content-shaped decision moves here, to a model, and the rules keep only
what a regex genuinely cannot get wrong: explicit commands, a bare note id, an
attachment, a correction the user already taught us.

One turn, no tools, JSON out. The engine still executes deterministically — the
model picks the capability and fills the slots, it does not touch the vault.
That distinction is the whole cost model: a decision is ~250 output tokens and
one round trip, where letting the model drive the tools measured 2-5 round
trips and 13-30s.

Measured on this machine (Claude Pro, `claude -p --model haiku`): ~6.5s median,
~$0.003 of plan allowance per call, 7/9 on a hand-built battery. Every failure
path here returns None, and None means the caller falls back to the old rules,
so a dead model degrades to the zero-cost tier rather than to an error.

Two thirds of what that call used to cost was context nobody here could use,
and it took measuring to see it (2026-08-22, same message, same model):

    baseline                     6.4s   19,554 prefix tokens   ~$0.010
    + --tools ""                 6.3s    7,012 prefix tokens   ~$0.003
    + --json-schema              9.9s   15,853 prefix tokens   ~$0.020

`--tools ""` is the win: `dontAsk` with no allowlist already *denied* every
tool, but denying a tool does not stop paying for its definition, and 12.5K
tokens of definitions were loaded on every classification that was forbidden
to call one. `--json-schema` looked like the obvious way to stop hand-parsing
the JSON below and is a loss on all three axes — it runs as an extra
validation pass (three turns, not one), so `_extract_json` stays.

Not visible in that table and worth as much as any of it: `stdin` must be
closed. Left open, the CLI waits three seconds for input that is never coming
— on every call, before the model is even reached.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings

# The catalogue the model picks from. Deliberately the router's own intent
# strings — a decision that doesn't name a real intent is a decision the engine
# cannot dispatch, so there is no translation layer to drift out of sync.
#
# `teach`, `recall`, `refile` and `brief` are *not* offered, and their absence
# is a safety property rather than an oversight. Each is already settled by a
# rule above this call, and each has a handler that reads slots the model has
# no way to fill: `_teach` with an empty `kind`/`text` would commit a junk
# belief to the memory store, which is the one wrong answer here that isn't
# cheap to undo. A decision the model cannot make well is one it isn't asked.
INTENTS = (
    "query", "tasks", "task.add", "task.done", "calendar", "schedule",
    "capture.note", "capture.lyric", "capture.task", "capture.place",
    "capture.person", "capture.workout", "capture.expense",
    "capture.decision", "capture.idea", "capture.link", "reminder",
    "chat", "smalltalk", "compound",
)

# The instruction rides in the *user* prompt, not `--append-system-prompt`.
# That flag appends to Claude Code's own system prompt rather than replacing
# it, so the model stays in assistant character: asked to classify "i should
# set a fixed time on sundays for my 1on1 with Arash" it replied "I'll help you
# set that up — what time works?" and never emitted JSON at all. The same words
# in the user turn get clean JSON back. (The Agent SDK *can* replace the system
# prompt, but it measured no faster and is a dependency this buys nothing.)
#
# Kept short on purpose: it is paid on every single decision. The rules in it
# are the ones the regexes got wrong, not a general description of the vault —
# the model does not need to know what a sprint note is to route a message.
PROMPT = """Classify the MESSAGE at the end. Output ONLY a JSON object — no
prose, no questions, no tool calls, no offers to help.

{"intent": "<one intent>", "confidence": 0.0-1.0, "reason": "<8 words max>", "slots": {}}

Intents: """ + ", ".join(INTENTS) + """

The distinctions that actually get confused:
- "I should X" / "I need to X" / "I must X" states a commitment -> task.add,
  EVEN IF X mentions a meeting, a person, a 1on1, or a day of the week.
  Wanting to fix or set a recurring meeting time is a task, not a calendar
  lookup. Put the commitment in slots.text.
- calendar = asking what is already scheduled. schedule = stating a specific
  dated event to record.
- tasks = asking about existing tasks. task.add = recording a new one.
- task.done = marking an existing one finished; put it in slots.target.
- query = a question about vault contents. chat = talking to V itself,
  including asking whether V did something.
- compound = several distinct actions in one message.
- Anything that is just thinking out loud -> capture.note.

Slots to fill when they apply:
- task.add -> {"text": "<the commitment, as an imperative>"}
- task.done -> {"target": "<the task named>"}
- tasks -> {"view": "open|priorities|overdue|backlog"}
- calendar -> {"view": "next"} only when asked what is next
- schedule -> {"title": "<what the event IS, 2-5 words>"}
  The title is the event, never the instruction. "for today calander set my
  meeting on 13:00 and not 17:30" is titled "Meeting" — not "calander set
  meeting and not". Drop scheduling verbs, dates, times, and any time the
  user is correcting away from. Keep who or what it is with.

Be decisive. Use confidence below 0.5 only when the message is genuinely
ambiguous, because a low score sends it to a dumber fallback, not to a human.

MESSAGE: """


@dataclass
class Decision:
    intent: str
    confidence: float
    reason: str
    slots: dict = field(default_factory=dict)
    ms: float = 0.0
    list_cost_usd: float = 0.0
    model: str = ""


def _extract_json(text: str) -> dict | None:
    """Pull the object out of whatever the model wrapped it in.

    Asking for "JSON and nothing else" gets JSON and nothing else most of the
    time; the rest of the time it gets a ```json fence or a sentence in front.
    Both are cheaper to handle than to re-prompt.
    """
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        out = json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


@dataclass
class Decider:
    """Asks a cheap model what a message is. Returns None rather than raising.

    None is load-bearing: it means "no opinion", and the router answers a no
    opinion with the heuristics it has always had. Nothing here is allowed to
    turn a working free path into a failure.
    """

    settings: Settings = field(default_factory=lambda: default_settings)

    def __post_init__(self) -> None:
        self.bin = shutil.which("claude")
        self.calls = 0
        self.failures = 0
        self.spend = 0.0

    @property
    def available(self) -> bool:
        # `BRAIN_NO_MODEL` is a hard kill switch that beats the config, and it
        # exists because the decider defaults to on: the test suite builds a
        # real Settings for every fixture, so without it `python -m
        # v.tests.test_core` quietly spends ~9s and real plan allowance
        # on each routing assertion. It doubles as the way to run V free and
        # offline for an afternoon without editing config.json.
        if os.environ.get("BRAIN_NO_MODEL"):
            return False
        return bool(self.bin) and self.settings.decider_enabled

    def decide(self, text: str) -> Decision | None:
        if not self.available or not (text or "").strip():
            return None

        cmd = [
            self.bin, "-p", PROMPT + text.strip(),
            "--output-format", "json",
            "--model", self.settings.decider_model,
            "--effort", "low",
            # No tools. This is a classification, not an errand: the agentic
            # probe that *did* have the vault tools spent 2-5 round trips and
            # 13-30s reaching the same conclusion. `dontAsk` with no allowlist
            # denies everything, which is exactly what we want — and note that
            # a bare `--allowed-tools` is not the way to say that: it takes a
            # value, so it silently swallows the next flag as its argument.
            "--permission-mode", "dontAsk",
            # Don't inherit the vault's project settings — they exist to give
            # the *agent* its MCP tools, and loading them here just pays for
            # tool definitions this call is forbidden from using. The empty
            # string is required; the flag errors out without a value.
            "--setting-sources", "",
            # …and the same argument, one level down, for the CLI's *built-in*
            # tools. `dontAsk` denies them; it does not unload them. Measured
            # at 12.5K tokens of definitions per call for Bash, Read, Edit and
            # the rest — none of which a classifier may touch. See the table
            # in the module docstring.
            "--tools", "",
            # Belt and braces on the same point: no MCP server, from any
            # scope, may attach to a classification. Nothing to start, nothing
            # to describe, nothing to wait for.
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            # A routing decision is not a conversation and will never be
            # resumed. Writing one transcript per message to disk is pure
            # cost.
            "--no-session-persistence",
        ]
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.settings.claude_config_dir)
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=self.settings.vault, capture_output=True, text=True,
                timeout=self.settings.decider_timeout, env=env,
                # Closed, not inherited. The CLI polls stdin for three seconds
                # before deciding nothing is coming, and it does that on every
                # single call — half the median latency of this path was spent
                # waiting for a pipe that was never going to carry anything.
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            self.failures += 1
            return None

        ms = (time.time() - t0) * 1000
        self.calls += 1
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.failures += 1
            return None

        payload = _extract_json(data.get("result") or "")
        if not payload:
            self.failures += 1
            return None

        intent = str(payload.get("intent", "")).strip()
        if intent not in INTENTS:
            # A hallucinated intent is worse than no answer: the engine would
            # fall through its dispatch chain and silently answer with a search.
            self.failures += 1
            return None

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        slots = payload.get("slots")
        cost = float(data.get("total_cost_usd") or 0.0)
        self.spend += cost

        return Decision(
            intent=intent,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(payload.get("reason", "") or "the model's call")[:80],
            slots=slots if isinstance(slots, dict) else {},
            ms=ms,
            list_cost_usd=cost,
            model=self.settings.decider_model,
        )

    def report(self) -> dict:
        return {"calls": self.calls, "failures": self.failures,
                "list_cost_usd": round(self.spend, 4),
                "model": self.settings.decider_model,
                "available": self.available}
