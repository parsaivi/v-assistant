"""commands.py — the slash commands, and what each one is a shortcut for.

This table lived in `cli.py`, which was right for exactly as long as the
terminal was the only place you could type. It is not any more. `v hud` has a
prompt too, and a command table that exists in one surface is a command that
silently becomes a *paid model turn* in the other: `/new` typed into the HUD
was answered by Sonnet with "Ready, sir." and billed $0.10, while the session
it was supposed to end carried on underneath. The command was not refused and
it was not unknown — the surface simply had never heard of commands.

So the table is here, in `core/`, where both prompts read the same one and a
command added to it cannot reach only half of them.

**Every command is a shortcut for something V already understands in plain
language, and the third column is that phrasing.** The design brief is blunt
about it — "slash is a hint, not a language" — and the rule it implies is
worth stating because it is easy to break: never add a command without a
spoken equivalent. If it cannot be said, it does not belong here.

What is *not* here is the running of them. `cli.py` prints to a terminal,
`hud.py` returns JSON to a browser, and the two share the engine methods
underneath (`engine.brief()`, `engine.undo()`, `engine.status()`) rather than
a dispatcher that would have to know about both. The one thing they must
agree on is which commands exist, which is this file.
"""

from __future__ import annotations

import json
from pathlib import Path

#: `(command, what it does, what you could have said instead)`.
COMMANDS: list[tuple[str, str, str]] = [
    ("/why", "the route, cost and notes behind the last answer", "why did you say that"),
    ("/cost", "today, this session, and where it went", "what have you cost me"),
    ("/undo", "revert the last thing written to the vault", "no, take that back"),
    ("/mem", "what I hold about a person or a project", "what do you know about x"),
    ("/quiet", "no unprompted messages until tomorrow", "leave me alone"),
    ("/local", "local answers only until further notice", "stay offline"),
    ("/brief", "the morning brief", "what does my day look like"),
    ("/tasks", "what is actually on your plate", "what should I do today"),
    ("/nudges", "what the vault says you have let slip", "anything I am forgetting"),
    ("/evening", "the close-out draft for today", "what did I get done"),
    ("/status", "the vault, the indexes, the shell", "how are you doing"),
    ("/new", "end this conversation, write it up, start fresh", "that's it for now"),
    ("/copy", "put the last answer on the clipboard", ""),
    ("/clear", "clear the screen", ""),
    ("/help", "this", "what can you do"),
    ("/quit", "or Ctrl-D", "goodbye"),
]

REPL_HELP = """Anything that is not a command is a message to V — and every
command below is a shortcut for something you could have just said.

Tab completes, ↑/↓ walk the history, ←/→ and Ctrl-A/E/W edit the line.
In the window the wheel scrolls the transcript and dragging over it
copies what you dragged across — the text alone, without the frame
around it. Shift-drag is your terminal's own selection, and `/mouse`
hands the mouse back for good."""

#: `/quit` is a terminal idea; a browser tab has a close button. The rest of
#: the table works on both surfaces, so this is a subtraction rather than a
#: second list — a second list is how the two drift apart again.
NOT_ON_HUD = frozenset({"/quit"})


def split(line: str) -> tuple[str, str] | None:
    """`"/undo abc"` → `("undo", "abc")`. `None` if it is not a command.

    A bare `/` returns `("", "")` — the palette, which every surface renders
    its own way but all of them open on the same keystroke.
    """
    if not line.startswith("/"):
        return None
    cmd, _, rest = line[1:].partition(" ")
    return cmd.strip().lower(), rest.strip()


def suggest(prefix: str) -> list[tuple[str, str, str]]:
    """The palette, prefix-filtered. `ui.palette` and the HUD both draw this.

    Prefix-match on the command name alone, per the brief: matching the
    description too looks cleverer and makes the list jump around under you as
    you type a word that happens to appear in one.
    """
    prefix = prefix.lower()
    if not prefix.startswith("/"):
        return []
    return [c for c in COMMANDS if c[0].startswith(prefix)]


# --------------------------------------------------------------------------
# `/quiet` and `/local`, on disk


def prefs_path(state_dir: Path) -> Path:
    return state_dir / "surface.json"


def prefs(state_dir: Path) -> dict:
    """On disk rather than in the process because the two things these govern
    — proactive messages and escalation — are decided by the scheduler, which
    is a different process. A quiet switch that only quiets the window you
    typed it into is not a quiet switch.

    Which is also why they are shared across surfaces rather than per-surface:
    `/quiet` from the HUD has to silence Telegram too, or it has not done what
    it says.
    """
    try:
        return json.loads(prefs_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(state_dir: Path, **kw) -> dict:
    data = prefs(state_dir)
    data.update(kw)
    try:
        prefs_path(state_dir).write_text(json.dumps(data, indent=2),
                                         encoding="utf-8")
    except OSError:
        pass
    return data
