"""Tests for the reasoning shell and the Hermes agency layer.

Run:  python3 -m v.tests.test_shell

Nothing here calls a model. These test the wiring, the cost accounting and —
most importantly — that the zero-cost posture holds by default, because the
failure mode of this layer is silent spending rather than a crash.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import os
# Never let a test spend plan allowance or 9s on a model call. The
# decider ships enabled, and every fixture here builds a real
# Settings, so this has to be set before v.core is imported.
os.environ["BRAIN_NO_MODEL"] = "1"
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import config, deferred, shells
from v.core.engine import Engine
from v.core.hermes import Hermes
from v.core.shells import (ClaudeCodeShell, NullShell, ShellResult,
                                   ShellUnavailable)
from v.tests.test_core import make_vault

# The real checked-in settings file, not a fixture: the deny rules in it are
# half of what makes granting Write/Edit safe, so the test has to read the one
# the CLI actually passes with `--setting-sources project,local`.
# Resolved from the configured vault, not from this file's location.
# V no longer lives inside the vault, so `parents[2]` is the package
# root and not the notes — the same coupling `config.resolve_vault()`
# exists to break.
VAULT_SETTINGS = config.settings.vault / ".claude" / "settings.json"

PASS, FAIL = [], []


def test(fn):
    def run():
        try:
            fn()
            PASS.append(fn.__name__)
            print(f"  \x1b[32m✓\x1b[0m {fn.__name__}")
        except Exception as exc:
            FAIL.append((fn.__name__, exc, traceback.format_exc()))
            print(f"  \x1b[31m✗\x1b[0m {fn.__name__}: {exc}")
    run.fn = fn
    return run


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


class FakeShell(shells.Shell):
    """Records what it was asked, returns a canned answer. No network."""

    name = "fake"

    def __init__(self, result: ShellResult | None = None, fail: bool = False):
        self.calls = []
        self.fail = fail
        self.result = result or ShellResult("an answer", "fake", model="m",
                                            session_id="s1", turns=2, ms=10.0,
                                            list_cost_usd=0.25)

    @property
    def available(self) -> bool:
        return not self.fail

    def run(self, prompt, *, session_id=None, allow_writes=False, timeout=None,
            model=None, effort=None, memory="", agent="v", tier_floor=None,
            on_event=None):
        self.calls.append({"prompt": prompt, "session_id": session_id,
                           "allow_writes": allow_writes, "model": model,
                           "effort": effort, "memory": memory, "agent": agent,
                           "tier_floor": tier_floor, "on_event": on_event})
        if self.fail:
            raise ShellUnavailable("nope")
        # Surfaces are entitled to progress from any shell, so a double that
        # never emits would let a dead progress seam pass the suite.
        if on_event:
            on_event(shells.ShellEvent("start", detail="s1"))
            on_event(shells.ShellEvent("tool", tool="vault_search", n=1))
            on_event(shells.ShellEvent("text", text=self.result.text))
        return self.result


# ---------------------------------------------------------------------
@test
def the_default_shell_is_null_and_refuses_loudly():
    """A silent no-op would look exactly like a working system, just useless."""
    s = shells.NullShell()
    ok(not s.available)
    try:
        s.run("anything")
        raise AssertionError("NullShell should refuse, not return")
    except ShellUnavailable as exc:
        ok("claude-code" in str(exc),
           "the refusal must say how to turn a shell on")


@test
def zero_cost_holds_until_a_shell_is_turned_on():
    """The whole design rests on this: out of the box, nothing spends."""
    root, s = make_vault()
    try:
        eq(s.shell_provider, "null", "default provider must not spend money")
        e = Engine(s)
        ok(not e.hermes.available, "no shell should be available by default")
        r = e.ask("something no schema and no note will match, xyzzy plugh")
        ok(not r.route.startswith("agent:"),
           f"escalated with no shell configured: {r.route}")
        eq(r.cost, 0.0)
    finally:
        shutil.rmtree(root)


@test
def structured_answers_never_reach_the_agent():
    """Router-first is the cost model. A SELECT must never become a paid run."""
    root, s = make_vault()
    try:
        e = Engine(s)
        fake = FakeShell()
        e.hermes = Hermes(s, shell=fake)
        for q in ("which issues are in-review?", "best cafes with wifi",
                  "give me my standup", "what assignments are due?"):
            r = e.ask(q)
            ok(r.route.startswith("structured:"), f"{q!r} -> {r.route}")
        eq(fake.calls, [], "a free question was sent to the agent")
    finally:
        shutil.rmtree(root)


@test
def open_questions_do_reach_the_agent():
    root, s = make_vault()
    try:
        e = Engine(s)
        fake = FakeShell()
        e.hermes = Hermes(s, shell=fake)
        r = e.ask("why did I choose append-only writes over rewriting notes?")
        eq(len(fake.calls), 1, "an open question should escalate")
        eq(r.route, "agent:fake")
        ok("an answer" in r.text)
    finally:
        shutil.rmtree(root)


@test
def a_subscription_run_reports_allowance_not_spend():
    """`cost` means money billed. On a subscription that is genuinely zero, so
    the allowance figure has to travel separately or it reads as free."""
    root, s = make_vault()
    try:
        e = Engine(s)
        res = ShellResult("x", "fake", list_cost_usd=0.42, billing="subscription")
        e.hermes = Hermes(s, shell=FakeShell(res))
        r = e.ask("an open ended question about my life")
        eq(r.cost, 0.0, "a subscription run is not billed per call")
        eq(r.data["list_cost_usd"], 0.42, "allowance used must still be reported")
        eq(r.data["billing"], "subscription")
    finally:
        shutil.rmtree(root)


@test
def an_api_key_run_reports_real_spend():
    root, s = make_vault()
    try:
        e = Engine(s)
        res = ShellResult("x", "fake", list_cost_usd=0.42, billing="api-key")
        e.hermes = Hermes(s, shell=FakeShell(res))
        r = e.ask("an open ended question about my life")
        eq(r.cost, 0.42, "an API-key run is real money and must show as cost")
    finally:
        shutil.rmtree(root)


@test
def the_length_ceiling_lifts_only_when_he_asks_for_detail():
    """The default reply is one or two sentences (`identity.py`, "## Length"),
    and the one thing that must still work is asking for more. The lift is
    sent in the turn rather than by swapping the system prompt, because
    `SYSTEM` has to stay byte-identical to keep its cache — so this asserts on
    the turn, which is where `shells.py` actually reads it."""
    root, s = make_vault()
    try:
        e = Engine(s)
        fake = FakeShell()
        e.hermes = Hermes(s, shell=fake)

        e.ask("explain how you rebuilt the docx")
        eq(len(fake.calls), 1)
        ok("ceiling does not apply" in fake.calls[0]["memory"],
           "asking for an explanation must lift the ceiling")

        # A control that still reaches the model — "is MR-1648 merged" would
        # be answered for free by `_structured` and never get here, which
        # would make this half of the test pass for the wrong reason.
        fake.calls.clear()
        e.ask("what did I decide about append-only writes")
        eq(len(fake.calls), 1)
        ok("ceiling does not apply" not in fake.calls[0]["memory"],
           "an ordinary question must stay under the ceiling")
    finally:
        shutil.rmtree(root)


@test
def the_length_rule_reaches_the_prompt():
    """The rule is worth nothing if it is not in the prompt the shell sends.
    Paired with the voice check in `test_memory.py`: that one asserts V
    sounds like itself, this one asserts it stops talking."""
    from v.core import identity, router
    ok("## Length" in identity.SYSTEM, "the length rule must be in the prompt")
    ok("One or two sentences" in identity.SYSTEM)
    # The voice section is read after the operating rules and must not
    # contradict them — it defers to the ceiling rather than reasserting
    # its own idea of brevity.
    ok(identity.SYSTEM.index("## Length") < identity.SYSTEM.index("## Voice"),
       "the length rule must be read before the voice that has to obey it")
    for asked in ("explain that", "why did you do it that way", "in detail",
                  "walk me through the fix", "what did you do"):
        ok(router.wants_detail(asked), f"{asked!r} is a request for detail")
    for ordinary in ("when did I last squat", "how many issues are open",
                     "remind me why I filed ISSUE-64", "book a meeting friday"):
        ok(not router.wants_detail(ordinary),
           f"{ordinary!r} must not lift the ceiling")


@test
def a_voice_message_tells_the_model_it_read_a_transcript():
    """Without this, "can you hear me" arrives at the model with nothing
    marking it as speech-to-text output, and the honest answer — "I have no
    audio input" — reads as V denying the voice memo did anything, when the
    transcript in front of it *is* the proof it worked. `ask(..., voice=True)`
    is engine.py's `Engine.handle(message_type="voice")` path collapsed to
    one call, same as the other shell tests exercise `ask()` directly rather
    than going through the router."""
    root, s = make_vault()
    try:
        e = Engine(s)
        fake = FakeShell()
        e.hermes = Hermes(s, shell=fake)
        e.ask("can you hear me", voice=True)
        eq(len(fake.calls), 1)
        ok("voice memo" in fake.calls[0]["memory"],
           "the model must be told this text came from a transcript")
        ok("transcript" in fake.calls[0]["memory"])

        fake.calls.clear()
        e.ask("an open ended question about my life", voice=False)
        eq(len(fake.calls), 1)
        ok("voice memo" not in fake.calls[0]["memory"],
           "an ordinary text question must not carry the voice note")
    finally:
        shutil.rmtree(root)


@test
def a_dead_shell_degrades_instead_of_crashing():
    root, s = make_vault()
    try:
        e = Engine(s)
        e.hermes = Hermes(s, shell=FakeShell(fail=True))
        r = e.ask("an open ended question")   # must not raise
        ok(r.text, "a dead shell should still produce a response")
    finally:
        shutil.rmtree(root)


@test
def writes_are_off_unless_asked_for():
    root, s = make_vault()
    try:
        fake = FakeShell()
        h = Hermes(s, shell=fake)
        h.ask("do something")
        eq(fake.calls[0]["allow_writes"], False,
           "the agent must not get write tools by default")
        h.ask("do something", allow_writes=True)
        eq(fake.calls[1]["allow_writes"], True)
    finally:
        shutil.rmtree(root)


@test
def the_read_toolset_contains_no_write_tools():
    """Compared against the real write list, not by substring: `vault_remind`
    writes one reminder, `vault_reminders` only lists them, and "remind" is a
    prefix of both."""
    read_side = set(shells.READ_TOOLS) | set(shells.HOST_TOOLS)
    write_side = set(shells.WRITE_TOOLS) | set(shells.HOST_WRITE_TOOLS)
    overlap = read_side & write_side
    ok(not overlap, f"tools in both sets: {overlap}")
    ok(not (read_side & {"Write", "Edit", "NotebookEdit"}),
       "raw file writers must not be granted on a read-only run")
    for tool in shells.WRITE_TOOLS:
        ok(tool.startswith("mcp__vault__"),
           f"{tool} is not an MCP vault tool, so it skips the append-only path")
    # `HOST_WRITE_TOOLS` is the deliberate exception, and the thing that keeps
    # it honest is the deny rules, not this list. If a file writer ever appears
    # here that `.claude/settings.json` does not fence off the numbered folders
    # for, vault prose becomes editable in place and nothing else would say so.
    fenced = set(json.loads(
        (VAULT_SETTINGS).read_text())["permissions"]["deny"])
    for tool in shells.HOST_WRITE_TOOLS:
        if tool == "NotebookEdit":
            continue  # no .ipynb lives under a numbered folder
        ok(f"{tool}(./[0-9][0-9] - */**/*.md)" in fenced
           and f"{tool}(./[0-9][0-9] - */*.md)" in fenced,
           f"{tool} is granted but not denied on vault notes")


@test
def every_run_is_a_fresh_context_by_default():
    """Resuming re-sends the whole transcript every turn. Silently keeping one
    thread open means an unrelated question pays to re-read the last one."""
    root, s = make_vault()
    try:
        fake = FakeShell()
        h = Hermes(s, shell=fake)
        t0 = dt.datetime(2026, 8, 1, 9, 0)
        h.ask("first", now=t0)
        h.ask("something unrelated", now=t0 + dt.timedelta(minutes=2))
        eq(fake.calls[0]["session_id"], None)
        eq(fake.calls[1]["session_id"], None,
           "a second question must not silently resume the first")
    finally:
        shutil.rmtree(root)


@test
def continuing_is_opt_in_and_expires():
    root, s = make_vault()
    try:
        s.shell_session_minutes = 30
        fake = FakeShell()
        h = Hermes(s, shell=fake)
        t0 = dt.datetime(2026, 8, 1, 9, 0)
        h.ask("first", now=t0)
        h.ask("follow up", now=t0 + dt.timedelta(minutes=5), continue_=True)
        eq(fake.calls[1]["session_id"], "s1", "--continue should resume")

        h.ask("much later", now=t0 + dt.timedelta(hours=3), continue_=True)
        eq(fake.calls[2]["session_id"], None,
           "a cold thread is expensive to reopen — start fresh")
    finally:
        shutil.rmtree(root)


@test
def an_open_session_keeps_one_thread_for_every_path():
    """The bug this exists for: "whose calendar is free?" then "book him at
    15:30" were two threads, so the second had never heard of him."""
    root, s = make_vault()
    try:
        from v.core.session import SessionStore
        store = SessionStore(s)
        fake = FakeShell()
        h = Hermes(s, shell=fake)
        t0 = dt.datetime(2026, 8, 1, 9, 0)

        h.ask("before any session", now=t0)
        eq(fake.calls[0]["session_id"], None,
           "outside a session the default is still a fresh thread")

        store.start(now=t0)
        h.ask("give me Arash's calendar", now=t0 + dt.timedelta(minutes=1))
        h.ask("book him 15:30", now=t0 + dt.timedelta(minutes=2))
        eq(fake.calls[2]["session_id"], "s1",
           "an open session must resume without anyone passing continue_")

        # The clock is not what ends a conversation.
        h.ask("hours later, same subject", now=t0 + dt.timedelta(hours=6))
        eq(fake.calls[3]["session_id"], "s1",
           "a long pause is not you saying you were finished")

        store.close("new", now=t0 + dt.timedelta(hours=6))
        h.reset()
        h.ask("something else entirely", now=t0 + dt.timedelta(hours=6))
        eq(fake.calls[4]["session_id"], None,
           "ending the session must drop the thread, not keep billing it")
    finally:
        shutil.rmtree(root)


@test
def tiers_pick_model_and_effort_from_the_question():
    """Paying top-tier reasoning rates for "when did I last squat" is waste,
    and on a Pro plan it comes out of the same allowance as normal usage."""
    from v.core.shells import HEAVY, LIGHT, NORMAL, tier_settings, weigh
    root, s = make_vault()
    try:
        eq(weigh("when did I last squat?"), LIGHT)
        eq(weigh("how many open issues do I have?"), LIGHT)
        eq(weigh("summarise my week"), NORMAL)
        eq(weigh("what did I decide about pricing and why?"), NORMAL,
           "a why-question is synthesis, not a lookup")
        eq(weigh("analyze the inconsistencies across my weekly notes"), HEAVY)
        eq(weigh("compare how DUNG and Leviathan log decisions"), HEAVY)

        eq(tier_settings(LIGHT, s), ("haiku", "low"))
        eq(tier_settings(NORMAL, s), ("sonnet", "medium"))
        eq(tier_settings(HEAVY, s), ("sonnet", "high"))
    finally:
        shutil.rmtree(root)


@test
def a_lookup_opener_has_to_be_a_whole_word():
    """"do it" starts with "do i". Matched as a prefix it was tiered `light`,
    which on 2026-09-05 handed "carry on writing my internship report" to haiku
    with a 90-second ceiling; it timed out with nothing written."""
    from v.core.shells import LIGHT, NORMAL, weigh
    eq(weigh("do it"), NORMAL, "a follow-up instruction is not a lookup")
    eq(weigh("do it now"), NORMAL)
    eq(weigh("next one"), LIGHT, "the real openers must still match")
    eq(weigh("do I have anything due?"), LIGHT)


@test
def a_resumed_thread_never_tiers_below_what_it_is_continuing():
    """A follow-up is the shortest message in a conversation and carries none
    of the weight of the work it continues — and resuming costs *more* than
    starting fresh, because the transcript is re-sent first. Tiering "do it"
    on its own two words is how a report-writing thread ended up on haiku."""
    from v.core.shells import HEAVY, NORMAL, heavier
    eq(heavier("light", NORMAL), NORMAL, "the floor raises")
    eq(heavier(HEAVY, NORMAL), HEAVY, "and never discounts an escalation")
    eq(heavier("light", None), "light", "no thread, no floor")
    eq(heavier("light", "nonsense"), "light", "an unknown floor is ignored")

    root, s = make_vault()
    try:
        fake = FakeShell(ShellResult("done", "fake", session_id="s1",
                                     tier=HEAVY))
        h = Hermes(s, shell=fake)
        t0 = dt.datetime(2026, 9, 5, 1, 0)
        h.ask("audit every note for schema errors", now=t0)
        h.ask("do it", continue_=True, now=t0 + dt.timedelta(minutes=2))
        eq(fake.calls[1]["session_id"], "s1", "same thread")
        eq(fake.calls[1]["tier_floor"], HEAVY,
           "the follow-up inherits the tier of the work it continues")
    finally:
        shutil.rmtree(root)


@test
def an_explicit_model_overrides_the_tier():
    root, s = make_vault()
    try:
        fake = FakeShell()
        h = Hermes(s, shell=fake)
        h.ask("when did I last squat?", model="opus", effort="max")
        eq(fake.calls[0]["model"], "opus")
        eq(fake.calls[0]["effort"], "max")
    finally:
        shutil.rmtree(root)


@test
def every_run_is_logged_and_totalled():
    """Without this there is no answering 'why did I hit my limit on Tuesday'."""
    root, s = make_vault()
    try:
        h = Hermes(s, shell=FakeShell())
        now = dt.datetime.now()
        h.ask("one", now=now)
        h.ask("two", now=now)
        u = h.usage(days=7, now=now)
        eq(u["runs"], 2)
        ok("by_tier" in u, "usage should break down by tier")
        eq(u["list_cost_usd"], 0.5)
        eq(u["errors"], 0)
        ok((s.log_dir / "hermes.jsonl").exists(), "the run log is missing")
    finally:
        shutil.rmtree(root)


@test
def old_runs_fall_out_of_the_usage_window():
    root, s = make_vault()
    try:
        h = Hermes(s, shell=FakeShell())
        now = dt.datetime(2026, 8, 1, 12, 0)
        h.ask("ancient", now=now - dt.timedelta(days=30))
        h.ask("recent", now=now)
        eq(h.usage(days=7, now=now)["runs"], 1)
    finally:
        shutil.rmtree(root)


@test
def the_claude_shell_knows_whether_it_is_billed():
    import os
    root, s = make_vault()
    try:
        sh = ClaudeCodeShell(s)
        had = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            eq(sh.billing, "subscription",
               "with no API key set, runs draw on the claude.ai subscription")
            os.environ["ANTHROPIC_API_KEY"] = "sk-test"
            eq(sh.billing, "api-key", "an API key means real per-token money")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if had:
                os.environ["ANTHROPIC_API_KEY"] = had
    finally:
        shutil.rmtree(root)


@test
def build_honours_the_configured_provider():
    root, s = make_vault()
    try:
        eq(type(shells.build(s)), NullShell)
        s.shell_provider = "claude-code"
        eq(type(shells.build(s)), ClaudeCodeShell)
        s.shell_provider = "hermes"
        eq(type(shells.build(s)).__name__, "HermesShell")
        s.shell_provider = "nonsense"
        eq(type(shells.build(s)), NullShell, "an unknown provider must not spend")
    finally:
        shutil.rmtree(root)


@test
def hermes_shell_explains_itself_when_missing():
    root, s = make_vault()
    try:
        sh = shells.HermesShell(s)
        if sh.available:
            return                      # actually installed; nothing to assert
        try:
            sh.run("hi")
            raise AssertionError("should have refused")
        except ShellUnavailable as exc:
            msg = str(exc)
            ok("install" in msg.lower(), "say how to install it")
            ok("Max" in msg, "say that Pro cannot use its Anthropic OAuth path")
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------------
# The two failures that actually happen, and what now survives them.
# Neither can be reproduced by running the suite — you would have to exhaust a
# real plan window — so the shell is made to fail in the exact shape the log
# records, and the queue is checked against that.

LIMIT_ERROR = "You've hit your session limit \u00b7 resets 3am (Asia/Tehran)"
AUTH_ERROR = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _refusing(error: str, rate_limit: dict | None = None) -> FakeShell:
    return FakeShell(ShellResult("", "fake", error=error,
                                 rate_limit=rate_limit or {}))


@test
def a_refused_question_is_kept_rather_than_dropped():
    """The failure this replaced: the window reopened and nobody re-asked."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.hermes = Hermes(s, shell=_refusing(LIMIT_ERROR))
        r = e.ask("why did I choose append-only writes over rewriting notes?")
        ok(r.data.get("degraded"), "a refusal must still degrade to local search")
        held = e.deferred.all()
        eq(len(held), 1, "the question should have been parked")
        eq(held[0].reason, deferred.LIMIT)
        ok("I'll ask again at" in r.text,
           f"the answer must say when, not just why: {r.text[:200]!r}")
    finally:
        shutil.rmtree(root)


@test
def the_reset_time_comes_from_the_stream_when_there_is_one():
    """`resetsAt` is exact; the error text is a fallback, not the source."""
    now = dt.datetime(2026, 8, 22, 14, 0)
    at = now + dt.timedelta(hours=2)
    exact = deferred.reset_at(LIMIT_ERROR,
                              {"resetsAt": at.timestamp()}, now=now)
    eq(exact.replace(second=0, microsecond=0), at.replace(second=0, microsecond=0),
       "the stream's epoch should win over the words in the error")
    # Falling back to the text: 3am is tomorrow's 3am, never this morning's.
    text_only = deferred.reset_at(LIMIT_ERROR, None, now=now)
    eq(text_only.date(), (now + dt.timedelta(days=1)).date())
    eq(text_only.hour, 3)


@test
def only_failures_that_time_fixes_are_parked():
    """A wrong answer retried in four hours is wrong twice, four hours apart."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.hermes = Hermes(s, shell=_refusing("the model returned an empty result"))
        e.ask("why did I choose append-only writes over rewriting notes?")
        eq(e.deferred.all(), [], "an ordinary failure must not be queued")
        eq(deferred.reason_for(AUTH_ERROR), deferred.AUTH)
        eq(deferred.reason_for("tool call denied"), None)
    finally:
        shutil.rmtree(root)


@test
def a_parked_question_is_answered_when_the_window_opens():
    root, s = make_vault()
    try:
        e = Engine(s)
        q = "why did I choose append-only writes over rewriting notes?"
        e.hermes = Hermes(s, shell=_refusing(LIMIT_ERROR))
        e.ask(q)
        # The window reopens: the same question, a shell that now answers.
        e.hermes = Hermes(s, shell=FakeShell())
        # Step past the parked item's own retry time rather than guessing a
        # fixed offset. The reset the limit error names is tomorrow's 3am on a
        # clock `config` pins to Asia/Tehran, so a flat "+13h" from wall-clock
        # now is only past it for part of the day.
        parked = e.deferred.all()
        eq(len(parked), 1, "the refused question should have been parked")
        later = dt.datetime.fromisoformat(parked[0].retry_after) \
            + dt.timedelta(minutes=1)
        answers = e.answer_deferred(later)
        eq(len(answers), 1, "the parked question should have been re-asked")
        ok(q in answers[0].text,
           "an answer arriving hours later must carry its question")
        ok("an answer" in answers[0].text)
        eq(e.deferred.all(), [], "an answered question must leave the queue")
    finally:
        shutil.rmtree(root)


@test
def a_question_that_keeps_being_refused_is_given_up_out_loud():
    """Silence is the one outcome not allowed: three windows, then a sentence."""
    root, s = make_vault()
    try:
        e = Engine(s)
        q = "why did I choose append-only writes over rewriting notes?"
        e.hermes = Hermes(s, shell=_refusing(LIMIT_ERROR))
        e.ask(q)
        said = []
        for i in range(1, deferred.MAX_TRIES + 2):
            said += e.answer_deferred(dt.datetime.now() + dt.timedelta(days=i))
        eq(e.deferred.all(), [], "it must not sit in the queue forever")
        ok(any(r.route == "deferred:gave-up" for r in said),
           f"giving up has to be said: {[r.route for r in said]}")
    finally:
        shutil.rmtree(root)


@test
def a_run_cut_off_mid_answer_keeps_what_it_wrote():
    """Four runs on record hit the timeout and returned nothing at all."""
    root, s = make_vault()
    try:
        e = Engine(s)
        half = ShellResult("Two of the three issues are closed", "fake",
                           error="timed out after 240s", partial=True, turns=4)
        e.hermes = Hermes(s, shell=FakeShell(half))
        r = e.ask("why did I choose append-only writes over rewriting notes?")
        ok("Two of the three issues are closed" in r.text,
           f"the partial answer was thrown away: {r.text[:120]!r}")
        ok(r.route.endswith(":partial"), r.route)
        ok(r.data.get("degraded"), "a cut-off answer must still read as degraded")
        eq(e.deferred.all(), [], "a timeout is not the plan window's fault")
    finally:
        shutil.rmtree(root)


@test
def progress_reaches_the_surface_while_the_run_is_still_going():
    """`Spinner.step` and the aperture's arms were written for this and fed
    nothing for as long as a turn was one opaque call."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.hermes = Hermes(s, shell=FakeShell())
        seen = []
        e.progress = seen.append
        e.ask("why did I choose append-only writes over rewriting notes?")
        kinds = [ev.kind for ev in seen]
        ok("tool" in kinds, f"no tool event reached the surface: {kinds}")
        ok("start" in kinds, kinds)
    finally:
        shutil.rmtree(root)


@test
def the_stream_is_read_into_text_tools_and_the_rate_limit_window():
    """The events are real ones, recorded off `--output-format stream-json`.

    Worth testing without a subprocess: this parser is what stands between a
    killed run and a lost answer, and the case it exists for — a run that dies
    before its final payload — is by definition one where nothing else has the
    text.
    """
    live = shells._Live()
    seen = []
    events = [
        {"type": "system", "subtype": "init", "session_id": "abc",
         "tools": ["Read", "Bash"]},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 34},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__vault__vault_search",
             "input": {"query_text": "uuid primary keys"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Two of the three"}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": " are closed"}}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1787355000,
            "rateLimitType": "five_hour"}},
    ]
    for event in events:
        live.absorb(event, seen.append)

    eq(live.text, "Two of the three are closed",
       "the deltas are the only copy of the text of a run that gets killed")
    eq(live.session_id, "abc")
    eq(live.tools, ["mcp__vault__vault_search"])
    eq(live.rate_limit.get("resetsAt"), 1787355000,
       "the window's reset time is what a parked question is scheduled on")
    kinds = [e.kind for e in seen]
    eq(kinds.count("tool"), 1)
    eq(kinds.count("tool_result"), 1)
    ok("think" in kinds, kinds)
    tool = next(e for e in seen if e.kind == "tool")
    eq(tool.detail, "uuid primary keys",
       "a tool call needs to say what it is about, not just its name")


@test
def a_second_block_of_prose_does_not_run_into_the_first():
    """Salvaged text is assembled from deltas, and two turns' worth of it
    concatenated with no break reads as one broken sentence."""
    live = shells._Live()
    for event in (
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "First."}}},
        {"type": "stream_event", "event": {"type": "content_block_stop",
                                           "index": 0}},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "Second."}}},
    ):
        live.absorb(event, lambda _e: None)
    eq(live.text, "First.\n\nSecond.")


@test
def a_broken_progress_callback_cannot_cost_an_answer():
    def explode(_ev):
        raise RuntimeError("the surface is on fire")

    calls = []
    shells._emit(explode, shells.ShellEvent("tool", tool="vault_search"))
    shells._emit(None, shells.ShellEvent("tool"))
    shells._emit(calls.append, shells.ShellEvent("text", text="hi"))
    eq(len(calls), 1, "a working callback still receives its events")


@test
def each_tier_gets_its_own_patience():
    """One flat ceiling meant a two-word lookup could hang for five minutes."""
    root, s = make_vault()
    try:
        light = shells.timeout_for(shells.LIGHT, s)
        heavy = shells.timeout_for(shells.HEAVY, s)
        ok(light < heavy, f"light {light}s should be under heavy {heavy}s")
        eq(shells.timeout_for("nonsense-tier", s), s.shell_timeout,
           "an unknown tier falls back to the flat ceiling")
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------------
def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} shell tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
