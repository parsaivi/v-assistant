#!/usr/bin/env python3
"""Local CLI and REPL. `v.core.telegram` is the other transport.

    v repl                  interactive
    v ask "which issues are in-review?"
    v send "remind me to call mom at 9"
    v index --embed
    v brief | nudges | validate | status
    v scheduler             the proactive loop
    v telegram              the bot: messages, reminders, briefs
    v hermes "..."          ask the agent (costs allowance)
    v shell --use claude-code   turn the agent on
    v --plain status        no colour, no animation

Presentation lives in `core/ui.py`; this module decides *what* to say and
hands it over to be coloured, wrapped and animated. Colour turns itself off
when stdout is not a terminal, so piping any of these into a file or a pager
still gives plain text.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import random
import re
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v.core import calendar as calendar_mod  # noqa: E402
from v.core import commands as commands_mod  # noqa: E402
from v.core import config, index, schema, shells, stt, ui  # noqa: E402
from v.core import notes as notes_mod  # noqa: E402
from v.core import persona  # noqa: E402
from v.core import session as session_mod  # noqa: E402
from v.core.hermes import Hermes  # noqa: E402
from v.core.shells import ShellUnavailable  # noqa: E402
from v.core.engine import Engine  # noqa: E402
from v import __version__  # noqa: E402

# The last thing V said, in plain text — what `/copy` copies.
_LAST: str = ""
# The last Response object, which is the only thing `/why` can open. Deliberately
# one, not a history: everything durable is already a note in the vault, and a
# browsable log of past answers would be the dashboard this project decided not
# to build.
_LAST_RESP = None


def _cost_note(resp) -> str:
    # On a subscription `cost` is genuinely 0 — nothing is billed per run — but
    # allowance *was* consumed, and printing $0.0000 for a run that ate a chunk
    # of your weekly limit is the kind of true-but-useless number that hides a
    # problem until you hit the wall.
    listed = resp.data.get("list_cost_usd")
    if listed and resp.data.get("billing") == "subscription":
        return f"≈${listed:.4f} allowance"
    if listed:
        return f"${listed:.4f}"
    if resp.cost:
        return f"${resp.cost:.4f}"
    # Local. Printed as an exact zero rather than the word "free", because the
    # number is the claim: nothing was billed *and* nothing left the machine.
    return "$0.0000"


_MODEL_DATE = re.compile(r"-\d{8}$")


def _short_model(name: str) -> str:
    """`claude-haiku-4-5-20251001` → `haiku-4-5`.

    The cost line is read at a glance and the vendor prefix and release date
    are the two parts of a model id that never change the answer to "was this
    worth it". Shortened here rather than at the shell, which needs the exact
    id to call anything.
    """
    return _MODEL_DATE.sub("", name.removeprefix("claude-"))


def _voice_state(resp) -> str:
    """Which of the four colours V's ✦ takes for this answer.

    Read off the response rather than set by each call site, so a degraded
    answer looks degraded no matter which path produced it.
    """
    if resp.data.get("refused"):
        return "refused"
    if resp.data.get("degraded") or resp.route.endswith(":fallback"):
        return "degraded"
    conf = resp.data.get("confidence")
    if conf is not None and conf < 0.55:
        return "unsure"
    return "speaking"


def _print(resp, animate: bool = False) -> None:
    global _LAST, _LAST_RESP
    # Kept as markdown, not as the ANSI rendering: `/copy` should give you
    # something you can paste into a note, not a screenful of escape codes.
    _LAST = resp.render()
    _LAST_RESP = resp
    conf = resp.data.get("confidence")
    body = ui.answer(_LAST, state=_voice_state(resp), confidence=conf)
    if animate:
        ui.settle(body)
        # The cost line belongs to the answer but is not part of it, and
        # landing it in the same breath makes it read as a last sentence.
        ui.beat()
    else:
        print(body)
    print(ui.costline(
        # The model when one ran, the route when none did. Both answer the
        # same question — where did this come from — and printing both makes
        # the line long enough to stop being read.
        model=_short_model(resp.data["model"]) if resp.data.get("model")
        else resp.route,
        notes=len(resp.citations) or None,
        seconds=resp.ms / 1000,
        cost=_cost_note(resp),
        confidence=conf,
        extra=[e for e in (
            resp.data.get("tier", ""),
            f"{resp.data['turns']} turns" if resp.data.get("turns") else "",
        ) if e],
    ))


# Every command, and the sentence that does the same thing.
#
# Slash is a hint, not a language. The third column is not documentation — it
# is the contract: a command may not exist here unless V already understands it
# spoken, because the moment the shortcut is the only way in, the assistant has
# quietly become a CLI with a chat prompt attached.
# The table itself lives in `core/commands.py` — `v hud` has a prompt too, and
# a command list only the terminal can see is a command that becomes a paid
# model turn everywhere else. Re-exported here because this module's own name
# for it is what `setup_readline` and `tui` are handed.
COMMANDS = commands_mod.COMMANDS
REPL_HELP = commands_mod.REPL_HELP

_COMPLETIONS = [c for c, _, _ in COMMANDS] + ["/exit"]


def _end_session(engine: Engine, reason: str = "quit", *, quiet: bool = False) -> None:
    """Close the conversation and say where it went.

    Everything that can end a REPL comes through here — Ctrl-D, `/quit`, two
    Ctrl-Cs, `/new`, and the atexit hook that catches the rest — because a
    recap written on three of those five and not the other two is a recap you
    cannot trust to be there.
    """
    try:
        recap = engine.end_session(reason)
    except Exception as exc:            # never let bookkeeping block the exit
        print(ui.error(f"couldn't close the session: {type(exc).__name__}: {exc}"))
        return
    if not recap or quiet:
        return
    where = engine.journal.log_path(recap.day)
    turns = f"{recap.n} turn{'s' if recap.n != 1 else ''}"
    if recap.filed:
        print(ui.hint(f"session written to [[{where[:-3]}]] — {turns}"))
    else:
        # The vault write is the only part that can fail; the transcript it was
        # made from is already on disk, so say which file rather than pretend
        # the conversation is gone.
        print(ui.error(f"couldn't write the session to the vault — "
                       f"the transcript is at {recap.path}"))


def _capture(fn, *a, **kw) -> str:
    """Run something that prints, and hand back what it printed.

    The command handlers below write their views straight to stdout, which is
    exactly right for the scrolling prompt and useless to the window, where
    every line has to land inside a pane. Rather than write each view twice —
    and let the two drift — the window runs the same handler and catches its
    output. The captured string is already styled, which is what `Entry("raw")`
    exists to carry.
    """
    import contextlib
    import io

    buf = io.StringIO()
    ui.SPINNER_OFF = True
    try:
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
    finally:
        ui.SPINNER_OFF = False
    return buf.getvalue().rstrip("\n")


def _repl_window(engine: Engine, identity) -> int:
    """The design's terminal: a window, a live side pane, a floating palette.

    Everything the scrolling prompt does, plus the two things it structurally
    could not: a status region that does not scroll away with the log, and a
    command palette that updates on every keystroke rather than on Tab.
    """
    from v.core import tui

    st = index.stats(engine.s)
    panes = tui.Panes(
        notes=st["notes"],
        daily_cap=float(getattr(engine.s, "daily_budget_usd", 2.0)),
        model=engine.s.shell_provider if engine.hermes.available
        and engine.s.shell_auto_escalate else "local",
        online=bool(engine.hermes.available and engine.s.shell_auto_escalate),
    )

    def turn(line: str) -> list:
        global _LAST, _LAST_RESP
        resp = engine.handle(line)
        _LAST, _LAST_RESP = resp.render(), resp
        conf = resp.data.get("confidence")
        listed = resp.data.get("list_cost_usd") or 0.0
        panes.turns += 1
        panes.local_turns += 0 if resp.data.get("model") else 1
        panes.spent += listed
        if resp.data.get("commit"):
            panes.writes += 1
            # Shown with the confidence it was written at. Nothing is held —
            # a capture still lands in the note immediately, by rule — so the
            # status here is `committed`, and the amber is a prompt to correct
            # it in a sentence rather than a queue waiting on you.
            panes.pending.insert(0, (resp.data.get("target") or "vault",
                                     conf if conf is not None else 1.0,
                                     "committed"))
            del panes.pending[4:]
        return [
            tui.Entry("answer", resp.render(), state=_voice_state(resp),
                      confidence=conf),
            tui.Entry("cost", ui.costline(
                model=_short_model(resp.data["model"])
                if resp.data.get("model") else resp.route,
                notes=len(resp.citations) or None,
                seconds=resp.ms / 1000, cost=_cost_note(resp),
                confidence=conf,
                extra=[e for e in (resp.data.get("tier", ""),
                                   f"{resp.data['turns']} turns"
                                   if resp.data.get("turns") else "") if e])),
        ]

    def command(line: str) -> list:
        if line.split(" ")[0] in ("/quit", "/exit"):
            return []
        # The handlers below all measure with `ui.width()`. Inside the window
        # that has to mean the pane, not the terminal.
        tw = window._split()[0]
        ui.WIDTH_OVERRIDE = max(24, tw - len(ui.HANG))
        try:
            out = _capture(_repl_command, line, engine)
        finally:
            ui.WIDTH_OVERRIDE = None
        # `/clear` empties the log rather than writing an escape into a pane.
        if line.startswith("/clear"):
            window.log.clear()
            return []
        return [tui.Entry("raw", out)] if out.strip() else []

    hist_path = engine.s.state_dir / "repl_history"
    try:
        history = hist_path.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        history = []

    def save(lines: list[str]) -> None:
        try:
            hist_path.write_text("\n".join(lines[-2000:]), encoding="utf-8")
        except OSError:
            pass

    # `/mute` and `/ambient` are the window's own — the scrolling prompt has no
    # aperture to mute and no pane to walk — so they are appended here rather
    # than living in COMMANDS, where the fallback REPL would offer them and
    # then have nothing to do with them.
    window = tui.Tui(handle=turn, on_command=command,
                     commands=COMMANDS + tui.WINDOW_COMMANDS,
                     panes=panes, version=__version__, history=history,
                     on_exit=save)
    # The aperture's arms and the spinner's label now come from the run itself
    # rather than from a constant. Set for the life of the window: unlike the
    # scrolling prompt, there is no spinner here to outlive.
    engine.progress = window.progress
    window.say(tui.Entry("plain", f"_{identity.TAGLINE}. "
                                  f"{st['notes']} notes indexed._"))
    if engine.startup_commit:
        c = engine.startup_commit
        window.say(tui.Entry("plain", f"_Auto-committed {c.rel} for you "
                                      f"(`{c.commit}`) — `/undo {c.commit}` "
                                      f"to take it back._"))
    code = window.run()
    _end_session(engine)
    return code


def cmd_repl(args, engine: Engine) -> int:
    from v.core import identity, tui

    ui.setup_readline(history=engine.s.state_dir / "repl_history",
                      words=_COMPLETIONS, describe=COMMANDS)

    # Before the banner: this also flushes any session a previous run died
    # holding, and those belong in their own day's log.
    for orphan in engine.start_session():
        print(ui.hint(f"filed an unfinished session from {orphan.day} "
                      f"({orphan.n} turn{'s' if orphan.n != 1 else ''})"))
    # A hard kill (`kill`, a closed terminal, an unhandled exception) skips
    # every `return` below, and losing the conversation is the one outcome this
    # whole feature exists to prevent. `atexit` catches those, and running it
    # after a clean exit is harmless: closing marks the session closed first,
    # so the second call finds nothing open and writes nothing. SIGTERM has to
    # be translated into a normal exit for atexit to run at all — the default
    # handler doesn't unwind.
    atexit.register(_end_session, engine, "interrupted", quiet=True)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except ValueError:
        pass                            # not the main thread; nothing to do

    # No greeting, and no status dump. V does not introduce himself to the
    # person who wrote him, and printing the whole index report before a word
    # has been typed answers a question nobody asked — `/status` is right
    # there. What is left is machine state: version, size, shell, and whether
    # anything is currently switched off.
    prefs = _prefs(engine)
    if prefs.get("local_only"):
        engine.s.shell_auto_escalate = False
        engine.router.agentic = False

    # The window is the interface. The scrolling prompt below is the fallback
    # for everywhere it cannot be drawn — a pipe, `--plain`, a dumb terminal,
    # a platform without termios — and those all still have to work.
    if tui.usable() and not getattr(args, "plain_repl", False):
        return _repl_window(engine, identity)

    st = index.stats(engine.s)
    shell = (f"{ui.G_ROUTE} {engine.s.shell_provider}" if engine.hermes.available
             and engine.s.shell_auto_escalate else f"{ui.G_LOCAL} local only")
    print(ui.banner(identity.NAME, identity.TAGLINE))
    print(ui.boot(__version__, st["notes"], shell))
    # Mentioned, not flagged. It is done and needs nothing from you — a `!`
    # reads as "you should commit these", which is the opposite of what
    # happened and turns an automatic step into a chore.
    if engine.startup_commit:
        c = engine.startup_commit
        print(ui.hint(f"auto-committed {c.rel} for you ({c.commit}) — "
                      f"`/undo {c.commit}` to take it back"))
    if prefs.get("quiet_until", "") > dt.datetime.now().isoformat():
        print(ui.hint(f"quiet until {prefs['quiet_until'][:16].replace('T', ' ')}"))
    print(ui.hint("/ for commands · Ctrl-D to quit"))
    print()

    interrupted = False
    while True:
        try:
            line = input(ui.prompt()).strip()
            interrupted = False
        except EOFError:
            print()
            _end_session(engine)
            print(f"{ui.hint('bye, sir.')}\n")
            return 0
        except KeyboardInterrupt:
            # One Ctrl-C throws the half-typed line away; two in a row means
            # you actually want out. Killing the session on the first press
            # punishes a fat-fingered keystroke.
            if interrupted:
                print()
                _end_session(engine)
                return 0
            interrupted = True
            print(f"\n{ui.hint('(^C again to quit)')}")
            continue

        if not line:
            continue
        if line in ("exit", "quit", "/quit", "/exit", "/q"):
            _end_session(engine)
            print(f"{ui.hint('bye, sir.')}\n")
            return 0
        if _repl_command(line, engine):
            print()
            continue

        try:
            resp = _thinking(engine.handle, line, engine=engine)
        except KeyboardInterrupt:
            print(ui.note("cancelled"))
            print()
            continue
        except Exception as exc:  # a bad message must not kill the session
            print(ui.error(f"{type(exc).__name__}: {exc}"))
            print()
            continue
        _print(resp, animate=True)
        print()


def _tool_label(name: str) -> str:
    """The tool's name as a person would say it, not as MCP spells it."""
    if name.startswith("mcp__claude-in-chrome__"):
        return "chrome · " + name.split("__")[-1]
    if name.startswith("mcp__vault__"):
        return name.split("__")[-1]
    return name


def _route_steps(spinner):
    """Turn a run's events into the route, printed above the spinner as it
    happens.

    The spec asks for a wait over four seconds to be legible rather than
    blank, and this is the whole of it: one line per tool call, in order, as
    they go out. Deliberately not everything the stream carries — a line per
    token would be a progress bar for the machine rather than for the person
    reading it, and the *route* is what `/why` will be asked about afterwards.
    """
    written = False

    def on_event(ev):
        nonlocal written
        if ev.kind == "tool":
            label = _tool_label(ev.tool)
            spinner.step(f"{label} · {ev.detail}" if ev.detail else label)
        elif ev.kind == "text" and not written:
            written = True
            spinner.step("writing the answer")
        elif ev.kind == "limit":
            spinner.step(f"plan window: {ev.detail}")

    return on_event


def _thinking(fn, *a, engine=None, **kw):
    """Run `fn` with a spinner turning next to it.

    The work stays on the main thread and only the animation is backgrounded.
    The other way round — engine on a worker, spinner in front — looks
    identical and doesn't work: the engine's SQLite connection belongs to the
    thread that opened it, so every call dies with "SQLite objects created in
    a thread can only be used in that same thread". Keeping the work here also
    means Ctrl-C lands on it, so cancelling actually cancels something.

    Pass `engine=` for anything that might escalate, and the spinner grows a
    route: the events arrive on the reader thread inside the shell while this
    thread is still blocked on the run.
    """
    with ui.Spinner(random.choice(ui.VERBS)) as spinner:
        if engine is not None:
            engine.progress = _route_steps(spinner)
        try:
            return fn(*a, **kw)
        finally:
            # Always cleared: `progress` outliving its spinner would write into
            # a line that is no longer being redrawn.
            if engine is not None:
                engine.progress = None


# `/quiet` and `/local` are read by the scheduler and written by whichever
# prompt you typed into, so they live in `core/commands.py` next to the table.
def _prefs(engine: Engine) -> dict:
    return commands_mod.prefs(engine.s.state_dir)


def _save_prefs(engine: Engine, **kw) -> dict:
    return commands_mod.save_prefs(engine.s.state_dir, **kw)


def _why(engine: Engine) -> None:
    """One answer, opened up: how it was reached, what it read, what it cost.

    Reached only from the answer it explains. There is no home for this view
    and no history behind it — everything durable is already a note, and
    growing this into something you can browse is how the vault stops being
    the dashboard.
    """
    resp = _LAST_RESP
    if resp is None:
        print(ui.note("nothing to explain yet"))
        return

    steps: list[tuple[str, str, float, str]] = []
    decision = resp.decision or "matched before any model was consulted"
    steps.append(("router", "local", 0.0,
                  f"{decision}. Rules run first and cost nothing, so the "
                  f"expensive path is only reached when they cannot answer."))
    route = resp.route or "unknown"
    if route.startswith("structured"):
        steps.append((route, "local", resp.ms / 1000,
                      "A SELECT over frontmatter. It is exact, and it cannot "
                      "invent a note that does not exist."))
    elif route.startswith("rag") or route.startswith("search"):
        steps.append((route, "local", resp.ms / 1000,
                      "Local embeddings and keyword search over note bodies. "
                      "Nothing was sent anywhere to answer this."))
    else:
        model = resp.data.get("model") or "the agent"
        steps.append((route, "model", resp.ms / 1000,
                      f"Escalated to {model} because no frontmatter schema "
                      f"matched — this needed judgement, not a lookup."))
    if resp.data.get("degraded"):
        steps.append(("fallback", "held", 0.0,
                      "The model could not be reached, so this is retrieved "
                      "evidence rather than a written answer."))
    print(ui.label("route"))
    print(ui.timeline(steps))
    print()

    print(ui.label("cost"))
    print(ui.kv([
        ("this answer", _cost_note(resp)),
        ("elapsed", f"{resp.ms:.0f}ms"),
        ("left the machine", "no" if resp.cost == 0 and not
         resp.data.get("list_cost_usd") else "yes"),
    ]))
    print()

    print(ui.label("notes read"))
    if resp.citations:
        for c in resp.citations[:8]:
            print(f"{ui.HANG}{ui.SEED}{ui.G_NOTE}{ui.RESET} "
                  f"{ui.PAPER}{c.strip('[]')}{ui.RESET}")
        print(ui.hint("every one of these is a real file — open it in Obsidian"))
    else:
        print(ui.hint("none — this came from V's own state, not from a note"))


def _cost_report(engine: Engine) -> None:
    u = engine.hermes.usage(7)
    today = engine.hermes.usage(1)
    print(ui.label("allowance · last 7 days"))
    print(ui.kv([
        ("today", f"≈${today['list_cost_usd']:.4f} over {today['runs']} run(s)"),
        ("7 days", f"≈${u['list_cost_usd']:.4f} over {u['runs']} run(s)"),
        ("average", f"{u['avg_ms']}ms" + (f" · {u['errors']} error(s)"
                                          if u["errors"] else "")),
        # On a subscription these two are what "expensive" actually means,
        # and neither shows up in a dollar figure. A low cache fraction is
        # prefix being rebuilt run after run; a high toolless count is
        # questions being handed a model that then answered from context
        # alone and never touched the vault — the tier was too generous.
        ("cached", f"{u['cached']:.0%} of prefix served from cache"),
        ("toolless", f"{u['toolless']} run(s) answered without a tool call"),
    ]))
    for tier, agg in sorted(u.get("by_tier", {}).items(),
                            key=lambda kv: -kv[1]["usd"]):
        frac = agg["usd"] / u["list_cost_usd"] if u["list_cost_usd"] else 0
        print(f"{ui.HANG}{ui.DIM2}{ui.pad(tier, 10)}{ui.RESET}"
              f"{ui.bar(frac, 18)} {ui.BODY_HI}${agg['usd']:.4f}{ui.RESET} "
              f"{ui.DIM3}{agg['runs']} run(s){ui.RESET}")
    print(ui.hint("everything not listed here was answered locally and cost "
                  "nothing at all"))


def _repl_command(line: str, engine: Engine) -> bool:
    """Slash commands. Returns True if the line was one."""
    if not line.startswith("/"):
        return False
    cmd, _, rest = line[1:].partition(" ")

    # A bare `/` is the palette. Typing one is the closest a readline prompt
    # gets to the design's live suggestion list — see `setup_readline`, which
    # puts the same table under Tab.
    if not cmd:
        print(ui.palette(COMMANDS))
        return True
    if cmd == "help":
        print(ui.palette(COMMANDS))
        print()
        print(ui.markdown(REPL_HELP))
    elif cmd == "why":
        _why(engine)
    elif cmd == "cost":
        _cost_report(engine)
    elif cmd == "undo":
        _print(engine.undo(rest.strip() or None))
    elif cmd == "mem":
        _print(_thinking(engine.handle,
                         f"what do you know about {rest}" if rest.strip()
                         else "what do you remember",
                         engine=engine), animate=True)
    elif cmd == "quiet":
        until = (dt.datetime.now() + dt.timedelta(days=1)).replace(
            hour=engine.s.quiet_hours[1], minute=0, second=0, microsecond=0)
        _save_prefs(engine, quiet_until=until.isoformat(timespec="seconds"))
        print(ui.ok(f"nothing unprompted until {until:%a %H:%M}. "
                    f"Reminders you set yourself still fire."))
    elif cmd == "local":
        on = not _prefs(engine).get("local_only")
        _save_prefs(engine, local_only=on)
        engine.s.shell_auto_escalate = not on
        engine.router.agentic = not on
        print(ui.ok("local only — nothing will leave this machine" if on
                    else "escalation back on"))
    elif cmd == "copy":
        if not _LAST:
            print(ui.note("nothing to copy yet"))
        else:
            how = ui.copy(_LAST)
            print(ui.ok(f"copied {len(_LAST)} chars via {how}") if how
                  else ui.error("no clipboard tool found "
                                "(install wl-clipboard or xclip)"))
    elif cmd == "new":
        # The escape hatch for the cost of remembering. A session that has run
        # all day re-sends all day on every turn, so when you change subject
        # entirely this is how you stop paying for the old one — and it files
        # what you're leaving behind rather than discarding it.
        _end_session(engine, "new")
        engine.start_session()
        print(ui.ok("fresh thread — nothing before this point carries over"))
    elif cmd == "clear":
        print("\x1b[2J\x1b[H", end="")
    elif cmd == "status":
        _print(engine.status())
    elif cmd == "brief":
        _print(_thinking(engine.brief), animate=True)
    elif cmd == "evening":
        _print(_thinking(engine.evening), animate=True)
    elif cmd == "nudges":
        _print(_thinking(engine.nudge_digest), animate=True)
    elif cmd == "tasks":
        _print(_thinking(engine.handle, "what should I do today",
                         engine=engine), animate=True)
    else:
        print(ui.error(f"unknown command /{cmd} — /help lists them"))
    return True


def cmd_send(args, engine: Engine) -> int:
    _print(_thinking(engine.handle, " ".join(args.text), engine=engine))
    return 0


def cmd_ask(args, engine: Engine) -> int:
    # Goes through `handle`, not `ask`. `ask` is only the retrieval half —
    # structured → semantic → agent — and calling it directly skips the router,
    # so `cli ask "what are my priorities"` escalated to a paid model while the
    # REPL answered the same words from SQL for free. Two doors into the same
    # assistant that behave differently is a bug wearing a feature's clothes.
    _print(_thinking(engine.handle, " ".join(args.text), engine=engine))
    return 0


def cmd_index(args, engine: Engine) -> int:
    label = "rebuilding embeddings" if args.embed else "indexing the vault"
    with ui.Spinner(label, delay=0.0):
        resp = engine.reindex(embed=args.embed, full=args.full)
    print(ui.ok(resp.text))
    return 0


def cmd_brief(args, engine: Engine) -> int:
    _print(_thinking(engine.brief), animate=True)
    return 0


def briefing_time() -> str:
    from v.core import briefing

    return briefing.DEFAULT_TIME.strftime("%H:%M")


def cmd_briefing(args, engine: Engine) -> int:
    """The standing 10:00 briefing — install it, inspect it, or push it now."""
    from v.core import briefing

    if args.install_cron:
        try:
            line = briefing.install_cron(engine.s)
        except RuntimeError as exc:
            print(ui.error(str(exc)))
            return 1
        print(ui.ok("standing briefing installed"))
        print(f"{ui.INDENT}{line}")
        return 0

    if args.uninstall_cron:
        removed = briefing.uninstall_cron()
        print((ui.ok if removed else ui.hint)(
            "standing briefing removed" if removed else "nothing was installed"))
        return 0

    if args.status:
        line = briefing.installed(engine.s)
        # Printed raw, not through `ui.markdown` — a cron spec is mostly
        # asterisks, and the emphasis parser eats them.
        if line:
            print(ui.markdown("**Schedule**"))
            print(f"{ui.INDENT}{line.split('  #')[0].strip()}")
        else:
            print(ui.markdown("**Schedule** — _not installed_ "
                              "(`cli briefing --install-cron`)"))
        state = briefing.last_run(engine.s)
        if not state:
            print(ui.hint("never run"))
            return 0
        verdict = ("delivered via " + ", ".join(state.get("channels") or [])
                   if state.get("delivered")
                   else f"not delivered — {state.get('reason', 'unknown')}")
        print(ui.markdown(f"**Last run** — {state.get('at')} · {verdict}\n\n"
                          f"signal {state.get('signal')} · "
                          f"{json.dumps(state.get('counts') or {})}"))
        return 0

    if args.push:
        brief = briefing.run(engine, force=args.force)
        if brief.delivered:
            print(ui.ok(f"pushed via {', '.join(brief.channels)}"))
        else:
            print(ui.hint(f"not pushed — {brief.reason}"))
        return 0

    # No flags: show what today's push would contain, and whether it would go.
    brief = _thinking(briefing.build, engine)
    print(ui.markdown(brief.text))
    print()
    print(ui.hint(f"signal {brief.signal} · "
                  + ("below the bar — this morning would stay silent"
                     if brief.thin else f"would push · {brief.headline()}")))
    return 0


def cmd_brag(args, engine: Engine) -> int:
    """Brag entries drafted from work that closed. Dry by default."""
    since = None
    if args.since:
        since = notes_mod.as_date(args.since)
        if since is None:
            print(ui.error(f"not a date: {args.since}"))
            return 1

    if args.pending:
        rows = engine.brag.pending_drafts()
        if not rows:
            print(ui.hint("no drafts waiting"))
            return 0
        print(ui.markdown("\n".join(f"- {r.link()}" for r in rows)))
        return 0

    with ui.Spinner("reading closed work", delay=0.0):
        report = engine.brag.run(dry_run=not args.apply, backfill=args.backfill,
                                 since=since, limit=args.limit)

    if report["seeded"]:
        print((ui.ok if args.apply else ui.hint)(
            f"{'seeded' if args.apply else 'would seed'} "
            f"{report['seeded']} already-closed items"))
        print(ui.hint("nothing drafted on a first run — `--backfill` to write "
                      "entries for work that closed before today"))
        return 0

    for d in report["drafted"]:
        target = d.get("wrote") or d.get("would_write")
        print(ui.markdown(f"**{'wrote' if args.apply else 'would write'}** "
                          f"`{target}`\n  from {d['rel']} — {d['why']}"))
    for sk in report["skipped"][:12]:
        print(ui.hint(f"skipped {sk['rel']} — {sk['why']}"))
    if not report["drafted"]:
        print(ui.hint("nothing new worth an entry"))
    elif not args.apply:
        print()
        print(ui.hint("dry run — add `--apply` to write these"))
    return 0


def cmd_evening(args, engine: Engine) -> int:
    _print(_thinking(engine.evening), animate=True)
    return 0


def cmd_nudges(args, engine: Engine) -> int:
    _print(_thinking(engine.nudge_digest), animate=True)
    return 0


def cmd_status(args, engine: Engine) -> int:
    _print(engine.status())
    return 0


def cmd_validate(args, engine: Engine) -> int:
    with ui.Spinner("checking every note against the schema", delay=0.0):
        report = schema.validate_vault(engine.s)
    print((ui.error if report.errors else ui.ok)(report.summary()))
    print()
    for rel, issues in sorted(report.by_file().items()):
        print(f"{ui.INDENT}{ui.BOLD}{rel}{ui.RESET}")
        for i in issues:
            colour = ui.RED if i.level == "error" else ui.YELLOW
            print(f"{ui.INDENT}  {colour}{i.field}{ui.RESET}: {i.message}")
        print()
    if args.fix_status:
        result = engine.work.fix_status_values(dry_run=not args.apply)
        print(ui.rule("status normalisation"))
        print(ui.hint(f"{result['count']} notes"
                      + (" — dry run, pass --apply to write"
                         if result["dry_run"] else " fixed")))
        for f in result["fixed"][:20]:
            print(f"{ui.INDENT}  {f['rel']}: {ui.DIM}{f['from']}{ui.RESET}"
                  f" → {ui.GREEN}{f['to']}{ui.RESET}")
    return 1 if report.errors else 0


def cmd_undo(args, engine: Engine) -> int:
    _print(engine.undo(args.commit))
    return 0


def cmd_search(args, engine: Engine) -> int:
    with ui.Spinner("searching"):
        res = engine.rag.search(" ".join(args.text), k=args.k)
    print(ui.tail([f"{len(res.hits)} hits", res.backend, f"{res.ms:.0f}ms"]))
    if res.stale:
        print(ui.note("stale index — re-run `index --embed`"))
    for h in res.hits:
        print()
        print(f"{ui.INDENT}{ui.ACCENT}{h.score:.3f}{ui.RESET} {ui.BOLD}{h.rel}"
              f"{ui.RESET}"
              + (f" {ui.DIM}›{ui.RESET} {ui.ACCENT_D}{h.heading}{ui.RESET}"
                 if h.heading else ""))
        print(f"{ui.INDENT}  {ui.DIM}{h.preview()}{ui.RESET}")
    return 0


def cmd_query(args, engine: Engine) -> int:
    q = engine.q
    name = args.name
    fn = {
        "issues": lambda: q.issues(status=args.status),
        "mrs": lambda: q.merge_requests(status=args.status),
        "stale": q.stale_reviews,
        "blocked": q.blocked,
        "workouts": q.workouts,
        "places": q.places,
        "people": q.people,
        "overdue": q.overdue_contacts,
        "birthdays": lambda: q.birthdays(60),
        "assignments": q.assignments,
        "goals": q.goals,
        "dormant": q.dormant_goals,
        "orphans": q.orphans,
        "broken": q.broken_links,
        "untyped": q.untyped,
        "empty": q.empty_notes,
        "dailies": q.daily_notes,
    }.get(name)
    if not fn:
        print(ui.error(f"unknown query: {name}"))
        return 2
    res = fn()
    print(ui.markdown(res.answer))
    print()
    print(ui.markdown(res.table(args.limit)))
    return 0


def cmd_tasks(args, engine: Engine) -> int:
    """The checkbox layer, from the shell."""
    import datetime as _dt
    day = _dt.date.fromisoformat(args.day) if args.day else _dt.date.today()

    if args.done:
        hits = engine.q.find_task(args.done)
        if not hits:
            print(ui.error(f"no open task matching {args.done!r}"))
            return 1
        if len(hits) > 1 and hits[0][1] - hits[1][1] < 0.15:
            print(ui.note(f"ambiguous — {args.done!r} matches:"))
            for t, sc in hits[:5]:
                print(f"{ui.INDENT}  {ui.DIM}[{t.id}] {sc:.2f}{ui.RESET} "
                      f"{t.render()}")
            return 1
        print(ui.ok(engine._complete(
            hits[0][0], _dt.datetime.combine(day, _dt.time())).text))
        return 0

    if args.backlog:
        r = engine.q.backlog(day)
        print(ui.markdown(r.answer))
        print()
        print(ui.markdown(r.table(20)))
        return 0

    view = ("overdue" if args.overdue
            else "open" if args.all else "priorities")
    r = {"overdue": lambda: engine.q.overdue_tasks(day, limit=args.limit),
         "open": lambda: engine.q.open_tasks(area=args.area, limit=args.limit),
         "priorities": lambda: engine.q.priorities(day, limit=args.limit,
                                                   area=args.area)}[view]()
    print(ui.markdown(r.answer))
    print()
    for row in r.rows:
        t = row.extra["_task"]
        print(f"{ui.INDENT}{ui.DIM}[{t.id}]{ui.RESET} {t.render(today=day)}")
        print(f"{ui.INDENT}      {ui.DIM}{t.rel}{ui.RESET}")
    return 0


def cmd_calendar(args, engine: Engine) -> int:
    """The vault calendar — read it, add to it, or push it into daily notes."""
    import datetime as _dt
    c = engine.q.calendar
    day = _dt.date.fromisoformat(args.day) if args.day else _dt.date.today()

    if args.add:
        ev = calendar_mod.parse_natural(" ".join(args.add), today=day)
        if not ev:
            print(ui.error("couldn't find a date and time in that"))
            return 1
        print(ui.ok(c.add(ev)))
        c.sync_daily_note(ev.date)
        return 0

    if args.sync:
        # Push the calendar into every daily note in the window, so the
        # `## 📅 Calendar` sections stop being a lie the template tells.
        n = 0
        with ui.Spinner("syncing daily notes"):
            for i in range(args.days):
                if c.sync_daily_note(day + _dt.timedelta(days=i)):
                    n += 1
        print(ui.ok(f"synced {n} daily note(s)"))
        return 0

    if args.import_ical:
        with ui.Spinner("pulling the feed", delay=0.0):
            added, skipped = c.import_ical(day, args.days)
        if not added and not skipped:
            print(ui.note("no iCal feed configured — nothing to import"))
            return 0
        print(ui.ok(f"imported {added} event(s), {skipped} already there"))
        return 0

    r = engine.q.events(day, days=args.days)
    print(ui.markdown(r.answer))
    if r.rows:
        print()
        print(ui.markdown(r.table(50)))
    return 0


def cmd_gcal(args, engine: Engine) -> int:
    """The live Google account, as opposed to `calendar`, which is the vault."""
    import datetime as _dt

    from v.core import gcal as gcal_mod

    g = gcal_mod.GoogleCalendar(engine.s)
    day = _dt.date.fromisoformat(args.day) if args.day else _dt.date.today()

    try:
        if args.action == "auth":
            if not g.configured():
                print(ui.note("no OAuth client yet"))
                print(ui.markdown(
                    "1. Open **console.cloud.google.com/apis/credentials**\n"
                    "2. Enable the **Google Calendar API** for the project\n"
                    "3. *Create credentials* → *OAuth client ID* → "
                    "type **Desktop app**\n"
                    "4. Save the client id and secret as:\n"
                    f"   `{g._client_path}`\n"
                    '   `{"client_id": "…", "client_secret": "…"}`\n'
                    "   (the file the console downloads works as-is too)\n"
                    "5. Re-run `python3 -m v.cli gcal auth`\n\n"
                    f"Add `http://127.0.0.1:{gcal_mod.REDIRECT_PORT}/` as an "
                    "authorised redirect URI if the console asks."))
                return 1
            print(ui.hint("opening your browser for consent…"))
            try:
                account = g.authorize(
                    on_url=lambda u: print(ui.hint(f"if it didn't open: {u}")))
            except gcal_mod.PartiallyAuthorised as exc:
                print(ui.ok("signed in — the grant is saved, "
                            "you will not have to do that again"))
                print(ui.error(f"but the first API call failed:\n  {exc}"))
                print(ui.hint("enable the API at the link above, give it a "
                              "minute, then just run `v gcal calendars` — "
                              "no need to re-authorise"))
                return 1
            print(ui.ok(f"connected as {account or '(unknown account)'}"))
            return 0

        if args.action == "calendars":
            cals = g.calendars()
            print(ui.markdown(f"**{len(cals)} calendar(s)**"))
            for c in sorted(cals, key=lambda c: (not c["primary"], c["name"])):
                mark = ui.GREEN + "●" + ui.RESET if c["writable"] else ui.DIM + "○" + ui.RESET
                own = " (yours)" if c["primary"] else ""
                print(f"{ui.INDENT}{mark} {c['name']} "
                      f"{ui.DIM}<{c['id']}> {c['access']}{own}{ui.RESET}")
            print()
            print(ui.hint("● writable   ○ read-only"))
            return 0

        if args.action == "busy":
            who = " ".join(args.who or []) or "me"
            start = _dt.datetime.combine(day, _dt.time.min, tzinfo=engine.s.tz)
            ids = [c["id"] for c in g.resolve(who)] or [who]
            blocks = g.freebusy(ids, start, start + _dt.timedelta(days=args.days))
            for cid, rows in blocks.items():
                print(ui.markdown(f"**{cid}**"))
                if rows and rows[0][0] == "error":
                    print(ui.error(f"  not visible to this account ({rows[0][1]})"))
                elif not rows:
                    print(ui.hint("  no busy blocks — free, or nothing shared"))
                else:
                    for a, b in rows:
                        print(f"{ui.INDENT}{a:%Y-%m-%d %H:%M}–{b:%H:%M}")
            return 0

        if args.action == "add":
            text = " ".join(args.who or [])
            if not (text and args.at):
                print(ui.error("need a title and --at 'YYYY-MM-DD HH:MM'"))
                return 1
            when = _dt.datetime.fromisoformat(args.at)
            ev = g.add_event(
                text, start=when, calendar_id=args.calendar,
                attendees=[a.strip() for a in (args.invite or "").split(",")
                           if a.strip()])
            print(ui.ok(f"created: {ev.render()}"))
            print(ui.hint(ev.link))
            return 0

        # default: list
        who = " ".join(args.who or []) or "me"
        matches = g.resolve(who)
        if not matches:
            print(ui.error(f"no calendar matches {who!r}"))
            print(ui.hint("`python3 -m v.cli gcal calendars` lists what this "
                          "account sees; "
                          "for anyone else try `... gcal busy <email>`"))
            return 1
        if len(matches) > 1:
            print(ui.note(f"{who!r} matches {len(matches)} calendars:"))
            for c in matches:
                print(f"{ui.INDENT}{c['name']} {ui.DIM}<{c['id']}>{ui.RESET}")
            return 1
        cal = matches[0]
        start = _dt.datetime.combine(day, _dt.time.min, tzinfo=engine.s.tz)
        evs = g.events(calendar_id=cal["id"], start=start,
                       end=start + _dt.timedelta(days=args.days))
        print(ui.markdown(f"**{cal['name']}** — {day.isoformat()}"
                          + (f" +{args.days - 1}d" if args.days > 1 else "")))
        if not evs:
            print(ui.hint("nothing scheduled"))
            return 0
        for e in evs:
            d = e.start if e.all_day else e.start.date()
            print(f"{ui.INDENT}{ui.DIM}{d}{ui.RESET}  {e.render()}")
        return 0

    except gcal_mod.NotConfigured as exc:
        print(ui.error(str(exc)))
        print(ui.hint("`python3 -m v.cli gcal auth` walks through it"))
        return 1
    except gcal_mod.NotAuthorised as exc:
        print(ui.error(str(exc)))
        return 1
    except gcal_mod.GoogleCalendarError as exc:
        print(ui.error(str(exc)))
        return 1


def cmd_gmail(args, engine: Engine) -> int:
    """Setup only — search/read/draft/send are conversational, via the MCP
    tools in `mcp/server.py`, the same split `gcal` draws between `cli gcal
    auth` (admin) and asking V to check a calendar (conversation)."""
    from v.core import gmail as gmail_mod

    g = gmail_mod.Gmail(engine.s)

    if args.reset:
        if g._token_path.exists():
            g._token_path.unlink()
            print(ui.ok("disconnected — run `gmail auth` to reconnect"))
        else:
            print(ui.hint("not connected"))
        return 0

    try:
        if not g.configured():
            print(ui.note("no OAuth client yet"))
            print(ui.markdown(
                "If Calendar is already connected (`cli gcal auth`), this is "
                "the *same* client — nothing more to create, just continue.\n\n"
                "Otherwise:\n"
                "1. Open **console.cloud.google.com/apis/credentials**\n"
                "2. Enable the **Gmail API** for the project\n"
                "3. *Create credentials* → *OAuth client ID* → "
                "type **Desktop app**\n"
                "4. Save the client id and secret as:\n"
                f"   `{g._client_path}`\n"
                '   `{"client_id": "…", "client_secret": "…"}`\n'
                "5. Re-run `python3 -m v.cli gmail auth`\n\n"
                f"Add `http://127.0.0.1:{gmail_mod.REDIRECT_PORT}/` as an "
                "authorised redirect URI if the console asks — the same one "
                "Calendar uses."))
            return 1
        print(ui.hint("opening your browser for consent…"))
        account = g.authorize(
            on_url=lambda u: print(ui.hint(f"if it didn't open: {u}")))
        print(ui.ok(f"connected as {account or '(unknown account)'}"))
        return 0
    except gmail_mod.NotConfigured as exc:
        print(ui.error(str(exc)))
        print(ui.hint("`python3 -m v.cli gmail auth` walks through it"))
        return 1
    except gmail_mod.GmailError as exc:
        print(ui.error(str(exc)))
        return 1


def cmd_reminders(args, engine: Engine) -> int:
    if args.fire:
        fired = engine.due_reminders()
        if not fired:
            print(ui.hint("nothing due"))
        for r in fired:
            print(ui.markdown(r.render()))
            # Settled only now, after it is on screen — the terminal is a
            # surface like any other, and `due_reminders` no longer settles
            # anything it merely composed.
            rid = (r.data or {}).get("reminder_id")
            if rid is not None:
                engine.settle_reminder(rid)
        return 0
    pending = engine.reminders.pending()
    print(ui.hint(f"{len(pending)} pending"))
    for r in pending:
        print(f"{ui.INDENT}{ui.ACCENT_D}•{ui.RESET} {r.describe()}")
    return 0


def cmd_service(args, engine: Engine) -> int:
    """Install `cli telegram` as a systemd --user service, so the proactive
    loop (reminders every minute, the morning brief, and now the Gmail/Quera
    checks) runs continuously instead of only while a terminal happens to
    have it open. `briefing --install-cron` covers the 10:00 push either
    way; this is what makes everything *between* 10:00 actually arrive."""
    import shutil
    import subprocess

    # Two units, because they are two transports with two failure modes: the
    # phone should keep working when the Telegram token expires, and Telegram
    # should keep working when nothing is enrolled. `--unit serve` picks the
    # second; the default stays what it always was.
    name = "v-serve.service" if args.unit == "serve" else "v.service"
    unit_src = Path(__file__).resolve().parent / "systemd" / name
    unit_dst_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dst = unit_dst_dir / name

    if args.action == "uninstall":
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "disable", "--now", name],
                          capture_output=True)
        if unit_dst.exists():
            unit_dst.unlink()
            print(ui.ok("removed — `systemctl --user daemon-reload` to be tidy"))
        else:
            print(ui.hint("not installed"))
        return 0

    if not shutil.which("systemctl"):
        print(ui.error("no systemctl on this machine — this only works "
                       "under systemd (most Linux desktops; not macOS)"))
        print(ui.hint(f"run the command in a terminal you keep open instead, "
                      f"or use another process supervisor with:\n  {unit_src}"))
        return 1

    unit_dst_dir.mkdir(parents=True, exist_ok=True)
    unit_dst.write_text(unit_src.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True)
    print(ui.ok(f"installed and started — `systemctl --user status {name}` "
                f"to check on it"))
    if args.unit == "serve":
        # A user unit stops at logout unless lingering is on, and a phone that
        # works only while a laptop has someone logged into its desktop is not
        # the thing anyone asked for.
        print(ui.hint("`sudo loginctl enable-linger $USER` so it keeps "
                      "running when you're logged out"))
    else:
        print(ui.hint(f"secrets (TELEGRAM_BOT_TOKEN, GCAL_CLIENT_ID/SECRET, …) "
                      f"go in {Path.home()}/.config/v/env, one "
                      f"KEY=value per line — the unit reads it if present"))
    return 0


def cmd_scheduler(args, engine: Engine) -> int:
    from v.core.scheduler import Scheduler

    Scheduler(engine).run(once=args.once, interval=args.interval)
    return 0


def cmd_telegram(args, engine: Engine) -> int:
    """The Telegram transport — the interactive bot plus the proactive push."""
    from v.core import telegram

    if args.reset_owner:
        engine.s.telegram_owner_id = 0
        raw = telegram._read_config(engine.s)
        raw["telegram_owner_id"] = 0
        telegram._write_config(engine.s, raw)
        print(ui.ok("ownership cleared — the next private message to the bot "
                    "claims it"))
        print(ui.hint("message it right away: this window is open to "
                      "whoever gets there first"))
        return 0

    if not engine.s.telegram_bot_token:
        print(ui.error("no telegram_bot_token configured"))
        print(ui.hint("export TELEGRAM_BOT_TOKEN=... or add "
                      '"telegram_bot_token" to ' + str(engine.s.state_dir / 'config.json')))
        return 1

    print(ui.hint(
        f"owner: {'claimed (id ' + str(engine.s.telegram_owner_id) + ')' if engine.s.telegram_owner_id else 'UNCLAIMED — first DM to the bot wins it'}"))
    try:
        telegram.run(engine.s)
    except (KeyboardInterrupt, SystemExit):
        print("\nstopped")
    return 0


# ---- the phone -----------------------------------------------------------
ANDROID_DIR = Path(__file__).resolve().parent / "android"
ANDROID_ASSETS = ANDROID_DIR / "app" / "src" / "main" / "assets"

ANDROID_PACKAGE = "cloud.brain.v"
# The app's own directory on external storage. Both files that cross it are
# public by nature — a public key going out and a certificate coming back — so
# a world-readable path is the right place for them, and it is the one place
# `adb` can reach on a stock phone without root or a debuggable build.
ANDROID_EXCHANGE = f"/sdcard/Android/data/{ANDROID_PACKAGE}/files"
PUBKEY_FILE = "device-pubkey.pem"
CERT_FILE = "device-cert.pem"


def _adb() -> str | None:
    """The SDK copy first — it is the one this project installed and knows the
    version of — then whatever is on PATH."""
    import shutil

    local = Path.home() / "Android" / "platform-tools" / "adb"
    if local.exists():
        return str(local)
    return shutil.which("adb")


def _adb_run(*args: str, timeout: int = 60):
    import subprocess

    adb = _adb()
    if not adb:
        raise FileNotFoundError("adb is not installed")
    return subprocess.run([adb, *args], capture_output=True, text=True,
                          timeout=timeout)


def cmd_phone(args, engine: Engine) -> int:
    """Certificates for the phone surface. See `core/pki.py` for the model."""
    from v.core import pki

    s = engine.s
    action = args.action

    if action == "status":
        st = pki.status(s)
        rows = [
            ("pki", st["dir"]),
            ("ca", st["ca_fingerprint"] or "not created"),
            ("server", "expires " + st["server_expires"] if st["server"]
                       else "not issued"),
            ("bind", httpapi_bind(s)),
            ("port", str(s.phone_port)),
            ("tailnet", st["tailnet"] or "not on a tailnet"),
            ("addresses", ", ".join(st["hosts"])),
            ("devices", ", ".join(st["devices"]) or "none enrolled"),
        ]
        width = max(len(k) for k, _ in rows)
        for k, v in rows:
            print(f"  {ui.DIM2}{k.ljust(width)}{ui.RESET}  {v}")
        if not st["devices"]:
            print()
            print(ui.hint("nothing can connect until a device is enrolled: "
                          "`cli phone enrol pixel`"))
        return 0

    if action == "revoke":
        name = args.name or s.phone_device
        if pki.revoke(name, s):
            print(ui.ok(f"{name} revoked — its certificate is gone and it "
                        f"stops connecting immediately"))
            print(ui.hint("a running server picks this up on that phone's next "
                          "request, and drops the event stream it is holding — "
                          "no restart needed. The handshake still succeeds "
                          "(there is no CRL); the app is refused after it, and "
                          "shows \"no longer enrolled\". Re-enrol and rebuild "
                          "to restore it"))
            return 0
        print(ui.error(f"no device called {name}"))
        return 1

    # enrol (default). Idempotent for the CA and the server certificate;
    # deliberately destructive for the device, because the only reason to run
    # it twice is that the old key should stop working.
    name = args.name or s.phone_device
    pki.ensure_ca(s)
    pki.ensure_server(s, force=args.reissue_server)

    if not args.bundle_key:
        return _enrol_keystore(args, s, pki, name)

    device = pki.enrol(name, s)

    ANDROID_ASSETS.mkdir(parents=True, exist_ok=True)
    for dest, src in device.assets().items():
        (ANDROID_ASSETS / dest).write_bytes(src.read_bytes())
    host = _write_server_json(s, pki, device.name)

    print(ui.ok(f"{device.name} enrolled"))
    print()
    print(f"  {ui.DIM2}ca      {ui.RESET}{pki.fingerprint(device.ca)}")
    print(f"  {ui.DIM2}device  {ui.RESET}{pki.fingerprint(device.cert)}")
    print(f"  {ui.DIM2}assets  {ui.RESET}{ANDROID_ASSETS}")
    print(f"  {ui.DIM2}host    {ui.RESET}https://{host}:{s.phone_port}")
    print()
    print(ui.hint("--bundle-key compiles the private key into the APK, and an "
                  "APK is world-readable on Android — any installed app can "
                  "read it out. Prefer plain `enrol`, which leaves the key in "
                  "the phone's secure element."))
    return 0


def _write_server_json(s, pki, device_name: str) -> str:
    """Where the app should dial, written at enrol time rather than typed into
    the app, so the certificate's SANs and the address cannot disagree."""
    host = pki.tailnet_ip() or httpapi_bind(s)
    # Loopback is dropped: a phone can never reach this machine's 127.0.0.1, so
    # leaving it in only spends a probe timeout before the app gets to an
    # address that can work.
    fallbacks = [h for h in pki.local_hosts()
                 if h != host and not h.startswith("127.")]
    ANDROID_ASSETS.mkdir(parents=True, exist_ok=True)
    (ANDROID_ASSETS / "server.json").write_text(json.dumps({
        "host": host,
        "port": s.phone_port,
        "fallbacks": fallbacks,
        "device": device_name,
    }, indent=2), encoding="utf-8")
    return host


def _enrol_keystore(args, s, pki, name: str) -> int:
    """Enrol a key the phone generated and will never hand over.

    The exchange is two files over a USB cable: a public key out, a certificate
    back. Nothing secret crosses it in either direction, which is the whole
    improvement — the private half is generated inside the phone's secure
    element and has no API that returns it, so there is no longer a copy of the
    credential sitting in a world-readable APK for any installed app to read.
    """
    ca_path = pki.pki_dir(s) / "ca.crt"
    ANDROID_ASSETS.mkdir(parents=True, exist_ok=True)
    (ANDROID_ASSETS / "ca.crt").write_bytes(ca_path.read_bytes())
    host = _write_server_json(s, pki, name)

    # A key compiled into an earlier build is not a credential any more; it is
    # a spare copy of one. Leaving it in `assets/` would mean the next build
    # quietly shipped it again.
    removed = []
    for stale in ("device.pk8", "device.crt"):
        p = ANDROID_ASSETS / stale
        if p.exists():
            p.unlink()
            removed.append(stale)

    if args.pubkey:
        pub = Path(args.pubkey).read_text(encoding="utf-8")
    else:
        pub = _pull_pubkey()
        if pub is None:
            return 1

    crt = pki.sign_public_key(name, pub, s)
    print(ui.ok(f"{name} enrolled — the private key stayed on the phone"))
    if removed:
        print(ui.hint(f"removed from the build: {', '.join(removed)} — the key "
                      f"is no longer shipped in the APK"))
    print()
    print(f"  {ui.DIM2}ca      {ui.RESET}{pki.fingerprint(ca_path)}")
    print(f"  {ui.DIM2}device  {ui.RESET}{pki.fingerprint(crt)}")
    print(f"  {ui.DIM2}host    {ui.RESET}https://{host}:{s.phone_port}")
    print()

    if args.pubkey:
        print(ui.hint(f"copy {crt} to the phone as "
                      f"{ANDROID_EXCHANGE}/{CERT_FILE}"))
        return 0
    return _push_cert(crt)


def _pull_pubkey() -> str | None:
    """Fetch the public key the app wrote on first run."""
    import tempfile

    remote = f"{ANDROID_EXCHANGE}/{PUBKEY_FILE}"
    try:
        devices = _adb_run("devices")
    except FileNotFoundError:
        print(ui.error("adb is not installed"))
        print(ui.hint(f"export the key from the phone by hand and pass it "
                      f"with --pubkey, or install platform-tools"))
        return None
    if len([l for l in devices.stdout.splitlines()[1:] if "\tdevice" in l]) == 0:
        print(ui.error("no phone connected over USB"))
        print(ui.hint("plug it in and accept the debugging prompt, or pass "
                      "--pubkey <file> if you copied the key across yourself"))
        return None

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / PUBKEY_FILE
        got = _adb_run("pull", remote, str(local))
        if got.returncode != 0 or not local.exists():
            print(ui.error("the phone has not published a public key yet"))
            print(ui.hint("open V on the phone once — it generates the key in "
                          "the secure element on first run and writes the "
                          "public half here — then run this again"))
            return None
        return local.read_text(encoding="utf-8")


def _push_cert(crt: Path) -> int:
    remote = f"{ANDROID_EXCHANGE}/{CERT_FILE}"
    pushed = _adb_run("push", str(crt), remote)
    if pushed.returncode != 0:
        print(ui.error(f"could not write the certificate to the phone: "
                       f"{pushed.stderr.strip()[:200]}"))
        print(ui.hint(f"copy {crt} to {remote} by hand"))
        return 1
    print(ui.ok("certificate installed on the phone"))
    print(ui.hint("reopen V — it picks the certificate up and moves it into "
                  "app-private storage"))
    return 0


def httpapi_bind(s) -> str:
    from v.core import httpapi
    return httpapi.resolve_bind(s)


def cmd_serve(args, engine: Engine) -> int:
    """The phone transport: mutual TLS, chat, voice both ways, proactive SSE."""
    from v.core import httpapi, pki

    s = engine.s
    if not pki.enrolled(s):
        print(ui.error("no device is enrolled"))
        print(ui.hint("`cli phone enrol pixel` first — with no enrolled "
                      "certificate nothing can complete the handshake"))
        return 1
    # The CLI's own engine is not the one that will answer: `httpapi._Bridge`
    # opens its own on the thread that owns it, the same way `cli telegram`
    # does. Closing this one now keeps a second idle sqlite connection off the
    # database for however many days the server runs.
    engine.close()
    server = httpapi.VServer(s, bind=args.bind, port=args.port,
                             scheduler=not args.no_scheduler,
                             verbose=args.verbose)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    except httpapi.ApiError as exc:
        print(ui.error(exc.message))
        return 1
    return 0


def cmd_hud(args, engine: Engine) -> int:
    """The wide desktop surface, in a browser on this machine.

    `repl` draws one conversation in a window. This draws the day: the same
    conversation next to the agenda, the ranked tasks, the deadlines, what V
    has learned and what it wrote — which is a layout problem, and a terminal
    has nowhere to put it.
    """
    from v.core import hud as hud_mod

    s = engine.s
    # Same reason as `cmd_serve`: the engine that answers is the one
    # `hud._Bridge` opens on its own thread, so this one is a second idle
    # sqlite connection for however long the HUD is left open.
    engine.close()
    try:
        port = args.port if args.port else hud_mod.free_port()
    except hud_mod.HudError as exc:
        print(ui.error(exc.message))
        return 1

    def ready(url, _srv):
        print(ui.hint(f"HUD on {url}"))
        print(ui.hint("loopback only · Ctrl-C to stop"))

    try:
        code = hud_mod.serve(s, port=port, open_browser=not args.no_open,
                             verbose=args.verbose, on_ready=ready)
    except OSError as exc:
        # A `finally` here would print "stopped" under the error too, which
        # reads as though it had been running.
        print(ui.error(f"could not listen on 127.0.0.1:{port}: {exc}"))
        return 1
    print("\nstopped")
    return code


def cmd_stt(args, engine: Engine) -> int:
    t = stt.Transcriber(engine.s, model_size=args.model)
    if args.benchmark:
        rows = t.benchmark(args.audio, sizes=tuple(args.sizes.split(",")))
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not args.audio:
        print(ui.error("pass an audio file, or --benchmark"))
        return 2
    src = Path(args.audio)
    if src.suffix.lower() in (".ogg", ".oga", ".m4a", ".mp3", ".opus"):
        src = stt.convert_to_wav(src)
    with ui.Spinner("transcribing", delay=0.0):
        result = t.transcribe(src, language=args.language)
    print(ui.tail([result.summary()]))
    print()
    print(ui.markdown(result.text))
    return 0


def cmd_work(args, engine: Engine) -> int:
    if args.standup:
        print(ui.markdown(engine.work.standup()))
        return 0
    with ui.Spinner("asking glab", delay=0.0):
        report = engine.work.sync(dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    return 0


def cmd_quera(args, engine: Engine) -> int:
    from v.core import quera as quera_mod

    q = quera_mod.Quera(engine.s, engine.vault)

    if args.action == "auth":
        if not args.cookie:
            print(ui.error("need --cookie — log into quera.org in a browser, "
                           "open devtools → Network, click any request "
                           "to the site, copy the whole Cookie request "
                           "header, and pass it here in quotes"))
            return 1
        q.save_cookie(args.cookie)
        print(ui.ok(f"saved — {q._cookie_path}"))
        return 0

    try:
        with ui.Spinner("checking Quera", delay=0.0):
            report = q.sync(dry_run=not args.apply)
    except quera_mod.NotAuthorised as exc:
        print(ui.error(str(exc)))
        return 1
    except quera_mod.QueraError as exc:
        print(ui.error(str(exc)))
        return 1
    if report.get("error"):
        print(ui.error(report["error"]))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    if report["dry_run"] and (report["created"] or report["updated"]):
        print(ui.hint("dry run — pass --apply to write"))
    return 0


def cmd_collision(args, engine: Engine) -> int:
    c = engine.rag.collision(seed=args.seed)
    if not c:
        print(ui.hint("no collision found"))
        return 0
    anchor, old, score = c
    print(ui.markdown(anchor))
    print(f"{ui.INDENT}{ui.ACCENT}↕ {score:.0%}{ui.RESET}")
    print(ui.markdown(old))
    return 0


def cmd_hermes(args, engine: Engine) -> int:
    """The agentic tier. Everything else in this CLI is free; this is not."""
    h = Hermes(engine.s)
    prompt = " ".join(args.text)

    if not h.available:
        print(ui.error(f"no shell available — provider="
                       f"{engine.s.shell_provider}"))
        print(ui.hint("turn one on:  python3 -m v.cli "
                      "shell --use claude-code"))
        return 1

    # The whole point of router-first: don't pay for what a SELECT answers.
    if not args.force:
        free = engine.ask(prompt)
        if free.route.startswith("structured:"):
            print(ui.markdown(free.render()))
            print(ui.tail([free.route, "answered for free",
                           "--force to ask the agent anyway"]))
            return 0

    try:
        # The one place a spinner really earns itself: an agent turn is
        # seconds to minutes, and silence for that long reads as a hang.
        with ui.Spinner(f"asking {args.model or 'the agent'}",
                        delay=0.0) as spinner:
            r = h.ask(prompt, allow_writes=args.write,
                      continue_=args.continue_, model=args.model,
                      effort=args.effort, on_event=_route_steps(spinner))
    except ShellUnavailable as exc:
        print(ui.error(str(exc)))
        return 1

    if not r.ok and not (r.partial and r.text.strip()):
        print(ui.error(r.error))
        return 1
    ui.reveal(ui.markdown(r.text))
    print(ui.tail([
        r.tier, f"{r.provider}:{r.model}", f"{r.turns} turn(s)",
        f"{len(r.tools_used)} tool call(s)" if r.tools_used else "no tools",
        f"{r.cached_fraction:.0%} cached" if r.cache_read_tokens else "",
        f"{r.ms:.0f}ms", r.cost_note(),
        f"{len(r.denials)} tool call(s) denied" if r.denials else "",
        # Said plainly rather than left for him to notice: the answer above is
        # the part that got written before the run was stopped.
        f"cut off — {r.error}" if r.partial else "",
    ]))
    return 0


def cmd_persona(args, engine: Engine) -> int:
    """Show, or switch, the character V is wearing."""
    from v.core import persona as persona_mod

    s = engine.s
    if args.use:
        want = persona_mod.ALIASES.get(args.use.strip().lower(), "")
        if not want:
            print(ui.error(f"unknown persona {args.use!r} — choose from: "
                           f"{', '.join(persona_mod.PERSONAS)}"))
            return 1
        args.use = want
        target = persona_mod.get(want)
        # An agent with a surface list cannot be made the standing default:
        # every message from anywhere else would resolve to it, fail the gate
        # and fall back, which is a config that lies about what is happening.
        # It is summoned by name in the surface it belongs to instead.
        if target.surfaces:
            print(ui.error(
                f"{want} is reachable from {', '.join(target.surfaces)} only, "
                f"so it cannot be the default. Summon it by name there "
                f"— type \"{want}\" in `v repl`."))
            return 1
        path = s.state_dir / "config.json"
        raw = json.loads(path.read_text()) if path.exists() else {}
        raw["persona"] = args.use
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        # The system prompt is snapshotted at import so it stays a cacheable
        # prefix, so a running `serve`/`telegram` keeps the old character
        # until it restarts. Say so rather than letting him wonder why V is
        # still addressing him the same way.
        print(ui.ok(f"persona → {args.use}"))
        print(f"{ui.INDENT}{ui.DIM}restart `serve`/`telegram` to pick it up"
              f"{ui.RESET}")
        s = config.load()

    p = persona_mod.get(s.persona)
    from v.core import tts as tts_mod
    synth = tts_mod.synthesizer(s)

    def field(label: str, value: str) -> None:
        print(f"{ui.INDENT}{ui.DIM}{label:<10}{ui.RESET}{value}")

    print()
    print(ui.rule("persona"))
    resolved = persona_mod.ALIASES.get((s.persona or "").strip().lower(), "")
    field("character", f"{ui.BOLD}{p.key}{ui.RESET}"
          + (f" {ui.DIM}(config says {s.persona!r}, which is not a persona)"
             f"{ui.RESET}" if not resolved else
             f" {ui.DIM}(config says {s.persona!r}){ui.RESET}"
             if resolved != s.persona else ""))
    field("address", p.address)
    field("memory", f"{p.scope}"
          + (f" {ui.DIM}· reads {', '.join(p.reads_scopes)}{ui.RESET}"
             if p.reads_scopes else ""))
    field("reach", ", ".join(p.surfaces) if p.surfaces
          else f"{ui.DIM}every surface{ui.RESET}")
    field("model", f"{p.model} · {p.effort}" if p.model
          else f"{ui.DIM}whatever the router tiers it at{ui.RESET}")
    field("writes", p.writes + (f" {ui.DIM}— vault only; anything leaving it "
                                f"still confirms{ui.RESET}"
                                if p.writes == persona_mod.DIRECT else ""))
    others = []
    for k, other in persona_mod.PERSONAS.items():
        if k == p.key:
            continue
        others.append(k + (f" ({'/'.join(other.surfaces)})" if other.surfaces
                           else ""))
    field("others", ", ".join(others) or f"{ui.DIM}none yet{ui.RESET}")
    print()

    # The half that grows. Shown here rather than under `v memory` because
    # this is the page Parsa opens when V sounds wrong, and "what has it
    # taught itself about how to talk to me" is the answer more often than
    # "which character is configured".
    store = engine.traits
    if args.forget:
        if not store.forget(args.forget, reason="dropped from `v persona`"):
            print(ui.error(f"no voice trait #{args.forget}"))
            return 1
        print(ui.ok(f"dropped voice trait #{args.forget}"))
        print()

    print(ui.rule("learned"))
    lines = store.describe()
    if not lines:
        print(f"{ui.INDENT}{ui.DIM}nothing learned yet — V picks these up when "
              f"you tell it how you want it to sound{ui.RESET}")
    else:
        for line in lines:
            head, *rest = line.split("\n")
            print(f"{ui.INDENT}{head}")
            for r in rest:
                print(f"{ui.INDENT}{ui.DIM}{r.strip()}{ui.RESET}")
    print()
    print(ui.rule("fixed"))
    for rule in p.core:
        print(f"{ui.INDENT}{ui.DIM}·{ui.RESET} {rule}")
    print(f"{ui.INDENT}{ui.DIM}these outrank anything learned above"
          f"{ui.RESET}")
    print()
    print(ui.rule("voice"))
    field("provider", s.tts_provider)
    field("speaks", f"{p.spoken.voice} {ui.DIM}· {p.spoken.model}{ui.RESET}")
    field("fallback", f"{p.spoken.piper} {ui.DIM}· piper, local{ui.RESET}")
    ready = synth.available
    field("ready", f"{ui.GREEN}yes{ui.RESET}" if ready
                   else f"{ui.RED}no{ui.RESET} {ui.DIM}— no key and no local "
                        f"voice model{ui.RESET}")
    if args.say:
        try:
            audio = synth.synthesize(args.say)
        except Exception as exc:
            print(ui.error(f"{type(exc).__name__}: {exc}"))
            return 1
        out = s.state_dir / "persona-sample.ogg"
        out.write_bytes(tts_mod.to_ogg_opus(audio))
        fell = getattr(synth, "last_error", "")
        field("sample", f"{out} {ui.DIM}({len(audio)} bytes"
              + (f", fell back: {fell}" if fell else "") + f"){ui.RESET}")
    print()
    return 0


def cmd_shell(args, engine: Engine) -> int:
    """Inspect or switch the reasoning shell."""
    s = engine.s
    if args.use:
        if args.use not in shells.SHELLS:
            print(ui.error(f"unknown shell {args.use!r} — choose from: "
                           f"{', '.join(shells.SHELLS)}"))
            return 1
        path = s.state_dir / "config.json"
        raw = json.loads(path.read_text()) if path.exists() else {}
        raw["shell_provider"] = args.use
        if args.model:
            raw[f"tier_{args.tier}_model"] = args.model
        if args.effort:
            raw[f"tier_{args.tier}_effort"] = args.effort
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        print(ui.ok(f"shell → {args.use}"
                    + (f" · model {args.model}" if args.model else "")
                    + (f" · effort {args.effort}" if args.effort else "")))

    s = config.load()
    h = Hermes(s)

    def field(label: str, value: str) -> None:
        print(f"{ui.INDENT}{ui.DIM}{label:<10}{ui.RESET}{value}")

    print()
    print(ui.rule("shell"))
    field("provider", f"{ui.BOLD}{s.shell_provider}{ui.RESET}")
    field("available", f"{ui.GREEN}yes{ui.RESET}" if h.available
                       else f"{ui.RED}no{ui.RESET}")
    field("continue", "yes" if s.shell_continue_by_default
                      else f"no {ui.DIM}— each run is fresh{ui.RESET}")
    field("billing", str(getattr(h.shell, "billing", "n/a")))
    field("thread", h.current_session() or f"{ui.DIM}(new on next ask){ui.RESET}")

    store = session_mod.SessionStore(s)
    live = store.current()
    if live:
        n = len(store.turns(live.id))
        field("session", f"{ui.GREEN}open{ui.RESET} {ui.DIM}since "
                         f"{live.started[11:16]} · {n} turn"
                         f"{'s' if n != 1 else ''} · /new to cut it{ui.RESET}")
    else:
        field("session", f"{ui.DIM}none — the next `repl` opens one{ui.RESET}")

    print()
    print(ui.rule("tiers"))
    for t in shells.TIERS:
        model = getattr(s, f"tier_{t}_model")
        cin, cout = shells.price_of(model)
        rate = (f" {ui.DIM}· ${cin:g}/${cout:g} per Mtok{ui.RESET}"
                if cin else "")
        field(t, f"{ui.CYAN}{shells.resolve_model(model)}{ui.RESET} "
                 f"{ui.DIM}· effort {getattr(s, f'tier_{t}_effort')}{ui.RESET}"
                 f"{rate}")

    print()
    print(ui.rule("tools"))
    for label, opts in (("read", shells.READ_TOOLS),
                        ("write", shells.WRITE_TOOLS),
                        ("host", shells.HOST_TOOLS + shells.HOST_WRITE_TOOLS)):
        field(label, f"{len(opts)} tools")

    u = h.usage(args.days)
    print()
    print(ui.rule(f"last {u['days']}d"))
    field("runs", f"{u['runs']} {ui.DIM}· ≈${u['list_cost_usd']} "
                  f"· avg {u['avg_ms']}ms{ui.RESET}"
                  + (f" · {ui.RED}{u['errors']} errors{ui.RESET}"
                     if u["errors"] else ""))
    for tier, agg in sorted(u.get("by_tier", {}).items()):
        field(f"  {tier}", f"{ui.DIM}{agg['runs']} runs · ≈${agg['usd']}{ui.RESET}")
    if u["runs"] and s.shell_provider == "claude-code" \
            and getattr(h.shell, "billing", "") == "subscription":
        print()
        print(ui.hint("a subscription is not billed per run — that figure is "
                      "the list-rate equivalent of the plan allowance used"))
    return 0


def cmd_memory(args, engine: Engine) -> int:
    """Inspect what V has learned — the whole point is that it's inspectable."""
    m = engine.memory
    if args.forget:
        mem = m.get(args.forget)
        if not mem:
            print(ui.error(f"no memory #{args.forget}"))
            return 1
        m.forget(args.forget)
        engine._sync_memory_note()
        print(ui.ok(f"forgotten: {mem.text}"))
        return 0
    if args.add:
        mem = m.remember(" ".join(args.add), kind=args.kind,
                         why="added from the CLI", source="told")
        engine._sync_memory_note()
        print(ui.ok(f"{ui.DIM}#{mem.id} [{mem.kind}]{ui.RESET} {mem.text}"))
        return 0
    if args.why is not None:
        mem = m.get(args.why)
        if not mem:
            print(ui.error(f"no memory #{args.why}"))
            return 1
        print(ui.markdown(mem.describe()))
        print()
        print(ui.rule("history"))
        for e in m.history(args.why):
            when = dt.datetime.fromtimestamp(e["at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{ui.INDENT}{ui.DIM}{when}{ui.RESET}  "
                  f"{ui.ACCENT_D}{e['event']:<13}{ui.RESET} {e['detail'] or ''}")
        return 0

    rows = m.all(active_only=not args.all)
    st = m.stats()
    print(ui.tail([f"{st['active']} active",
                   f"{st['superseded_or_forgotten']} superseded or forgotten",
                   f"{st['faded']} faded below the injection threshold"]))
    print()
    for mem in rows:
        print(ui.markdown(mem.describe()))
        print()
    return 0


def cmd_docs(args, engine: Engine) -> int:
    """Regenerate V's self-documentation in `19 - Assistant/V/`."""
    from v.core import identity

    with ui.Spinner("writing V's notes"):
        engine.journal.refresh_all(
            capabilities=identity.capabilities(engine),
            goals=identity.GOALS,
            memory_markdown=engine.memory.export_markdown(),
        )
    print(ui.ok("wrote 19 - Assistant/V/ — README, Skills, Goals, Memory"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="v", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plain", action="store_true",
                   help="no colour, no animation (also: NO_COLOR=1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("repl")
    # The escape hatch out of the window and back to the scrolling prompt,
    # for when you want the terminal's own scrollback rather than V's.
    s.add_argument("--plain-repl", action="store_true",
                   help="the scrolling prompt instead of the window")
    s.set_defaults(fn=cmd_repl)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("brief").set_defaults(fn=cmd_brief)
    sub.add_parser("evening").set_defaults(fn=cmd_evening)
    sub.add_parser("nudges").set_defaults(fn=cmd_nudges)

    s = sub.add_parser("briefing", help="the standing 10:00 morning push")
    s.add_argument("--push", action="store_true",
                   help="deliver it now (what the crontab entry runs)")
    s.add_argument("--force", action="store_true",
                   help="push even if it's thin, muted, or already went today")
    s.add_argument("--status", action="store_true",
                   help="is it installed, and what did the last run do")
    s.add_argument("--install-cron", action="store_true",
                   help=f"install the {briefing_time()} crontab entry")
    s.add_argument("--uninstall-cron", action="store_true")
    s.set_defaults(fn=cmd_briefing)

    s = sub.add_parser("brag", help="draft brag entries from closed work")
    s.add_argument("--apply", action="store_true", help="actually write them")
    s.add_argument("--backfill", action="store_true",
                   help="also consider work that closed before V started watching")
    s.add_argument("--since", help="only work closed on or after this date")
    s.add_argument("--pending", action="store_true",
                   help="list drafts still waiting on their impact line")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_brag)

    s = sub.add_parser("send"); s.add_argument("text", nargs="+"); s.set_defaults(fn=cmd_send)
    s = sub.add_parser("ask"); s.add_argument("text", nargs="+"); s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("tasks", help="checkbox tasks, ranked")
    s.add_argument("--all", action="store_true", help="every open task")
    s.add_argument("--overdue", action="store_true")
    s.add_argument("--backlog", action="store_true", help="how far behind")
    s.add_argument("--done", metavar="TEXT", help="tick a task off by name")
    s.add_argument("--area", help="Work / University / Life")
    s.add_argument("--day", help="YYYY-MM-DD (default today)")
    s.add_argument("--limit", type=int, default=12)
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("calendar", help="the vault calendar")
    s.add_argument("--add", nargs="+", metavar="TEXT",
                   help='e.g. --add on Aug 8 at 12:30 1on1 with Sajad')
    s.add_argument("--sync", action="store_true",
                   help="render the calendar into daily notes")
    s.add_argument("--import-ical", action="store_true", dest="import_ical",
                   help="pull the remote feed into the vault calendar")
    s.add_argument("--day", help="YYYY-MM-DD (default today)")
    s.add_argument("--days", type=int, default=1)
    s.set_defaults(fn=cmd_calendar)

    s = sub.add_parser("gcal", help="the live Google Calendar, yours and others'")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "auth", "calendars", "busy", "add"])
    s.add_argument("who", nargs="*",
                   help="a name, an email, or 'me' — the title when adding")
    s.add_argument("--day", help="YYYY-MM-DD (default today)")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--at", help="start time when adding: 'YYYY-MM-DD HH:MM'")
    s.add_argument("--invite", help="comma-separated guest emails")
    s.add_argument("--calendar", default="primary",
                   help="calendar id to write to (default your own)")
    s.set_defaults(fn=cmd_gcal)

    s = sub.add_parser("gmail", help="connect Gmail (setup only — talk to V "
                                     "for search/read/send)")
    s.add_argument("--reset", action="store_true",
                   help="disconnect, to re-authorise onto a different account")
    s.set_defaults(fn=cmd_gmail)

    s = sub.add_parser("index")
    s.add_argument("--embed", action="store_true", help="also rebuild embeddings")
    s.add_argument("--full", action="store_true",
                   help="re-parse every note, ignoring mtime (use after a parser change)")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("search")
    s.add_argument("text", nargs="+"); s.add_argument("-k", type=int, default=8)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("query")
    s.add_argument("name"); s.add_argument("--status"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_query)

    s = sub.add_parser("validate")
    s.add_argument("--fix-status", action="store_true",
                   help="normalise GitLab status vocabulary (opened → open)")
    s.add_argument("--apply", action="store_true", help="actually write the fixes")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("undo"); s.add_argument("commit", nargs="?"); s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("reminders"); s.add_argument("--fire", action="store_true")
    s.set_defaults(fn=cmd_reminders)

    s = sub.add_parser("scheduler")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=30)
    s.set_defaults(fn=cmd_scheduler)

    s = sub.add_parser("telegram", help="the Telegram bot — messages in, "
                       "reminders and briefs out")
    s.add_argument("--reset-owner", action="store_true",
                   help="un-claim the bot so the next DM claims it again")
    s.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("serve", help="the phone transport — mutual TLS, chat, "
                       "voice both ways, proactive events")
    s.add_argument("--bind", default=None,
                   help="address to listen on (default: the tailnet interface, "
                        "then the LAN address — never 0.0.0.0)")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-scheduler", action="store_true",
                   help="don't run reminders/brief/nudges from this process")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("hud", help="the wide desktop surface — the day on one "
                       "screen, in a browser on this machine")
    s.add_argument("--port", type=int, default=None,
                   help=f"port on 127.0.0.1 (default: {7717} or the next free one)")
    s.add_argument("--no-open", action="store_true",
                   help="don't open a browser window")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="log every request")
    s.set_defaults(fn=cmd_hud)

    s = sub.add_parser("phone", help="certificates for the phone surface")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "enrol", "enroll", "revoke"])
    s.add_argument("name", nargs="?", help="device name (default: phone_device)")
    s.add_argument("--reissue-server", action="store_true",
                   help="reissue the server certificate even if its addresses "
                        "haven't changed")
    s.add_argument("--pubkey", metavar="FILE",
                   help="sign this PEM public key instead of pulling one off "
                        "the phone over adb")
    s.add_argument("--bundle-key", action="store_true",
                   help="the old scheme: generate the private key here and "
                        "compile it into the APK. Readable by any app on the "
                        "phone — see 19 - Assistant/Threat Model.md")
    s.set_defaults(fn=cmd_phone)

    s = sub.add_parser("service", help="run a transport continuously as a "
                       "systemd --user service")
    s.add_argument("action", nargs="?", default="install",
                   choices=["install", "uninstall"])
    s.add_argument("--unit", default="telegram", choices=["telegram", "serve"],
                   help="which transport (default: telegram)")
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("stt")
    s.add_argument("audio", nargs="?")
    s.add_argument("--model", default=None)
    s.add_argument("--language", default=None)
    s.add_argument("--benchmark", action="store_true")
    s.add_argument("--sizes", default="tiny,base,small,medium,large-v3")
    s.set_defaults(fn=cmd_stt)

    s = sub.add_parser("work")
    s.add_argument("--standup", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_work)

    s = sub.add_parser("quera", help="university deadlines from quera.org")
    s.add_argument("action", nargs="?", default="sync", choices=["auth", "sync"])
    s.add_argument("--cookie", help="the pasted `Cookie` header, for `auth`")
    s.add_argument("--apply", action="store_true",
                   help="write the notes (default is a dry run)")
    s.set_defaults(fn=cmd_quera)

    s = sub.add_parser("hermes", help="ask the agent (costs plan allowance)")
    s.add_argument("text", nargs="+")
    s.add_argument("--write", action="store_true",
                   help="let it write to the vault (append-only, git-committed)")
    s.add_argument("-c", "--continue", dest="continue_", action="store_true",
                   help="follow up on the last answer (default: each run is a "
                        "fresh context, so you don't pay to re-send history)")
    s.add_argument("--model", help="override the tier's model (haiku|sonnet|opus)")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   help="override the tier's effort")
    s.add_argument("--force", action="store_true",
                   help="skip the free structured path and go straight to the agent")
    s.set_defaults(fn=cmd_hermes)

    s = sub.add_parser("persona", help="show or switch V's character and voice")
    s.add_argument("--forget", type=int, metavar="ID",
                   help="drop a learned voice trait by id")
    s.add_argument("--use", help="which character to wear (e.g. jarvis)")
    s.add_argument("--say", metavar="TEXT",
                   help="synthesize a line and write it to state/ to listen to")
    s.set_defaults(fn=cmd_persona)

    s = sub.add_parser("shell", help="inspect or switch the reasoning shell")
    s.add_argument("--use", help="null | claude-code | hermes")
    s.add_argument("--tier", default="normal",
                   choices=["trivial", "light", "normal", "heavy"],
                   help="which tier --model/--effort apply to")
    s.add_argument("--model")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_shell)

    s = sub.add_parser("memory", help="what V has learned from you")
    s.add_argument("--all", action="store_true",
                   help="include superseded and forgotten memories")
    s.add_argument("--add", nargs="+", help="store a memory directly")
    s.add_argument("--kind", default="fact",
                   choices=["fact", "preference", "correction", "pattern"])
    s.add_argument("--forget", type=int, metavar="ID")
    s.add_argument("--why", type=int, metavar="ID",
                   help="show one memory's full history")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("docs", help="regenerate V's notes in 19 - Assistant/V/")
    s.set_defaults(fn=cmd_docs)

    s = sub.add_parser("collision"); s.add_argument("--seed", type=int); s.set_defaults(fn=cmd_collision)
    return p


def main(argv: list[str] | None = None) -> int:
    # No args means the REPL — `v` on its own should start talking, not print usage.
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["repl"]
    args = build_parser().parse_args(argv)
    if args.plain:
        ui.disable_colour()
    # The repl is the one surface that requires a keyboard on this machine,
    # and it is what EDITH is gated to. A one-shot `v ask` is local too but is
    # also what a cron line or any script with a shell runs, so it is `cli`.
    surface = persona.TUI if args.fn is cmd_repl else persona.CLI
    engine = Engine(config.load(), surface=surface)
    try:
        return args.fn(args, engine)
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
