"""tui.py — the terminal window from §03 of the design, drawn for real.

The REPL this replaces was a scrolling prompt: input, answer, input, answer,
straight down the terminal. The design is not that. It is a *window* — a title
bar with the model and the day's spend, a transcript pane, a live side pane
that never scrolls with the log, and a prompt with the command palette
floating above it. Half the information in the spec has nowhere to live in a
scrolling prompt, which is why it kept being left out.

So this takes the alternate screen. The cost of that is scrollback: your
terminal's own history stops applying, and PgUp/PgDn scroll the transcript
here instead.

It takes the mouse too, which it did not use to. The original reasoning was
that leaving the mouse alone kept the terminal's own select-to-copy working —
except that it did not, for two reasons that only show up once the window is
really drawn. The first is that a terminal on the alternate screen turns the
wheel into arrow keys (`?1007`, on by default in VTE), so scrolling the
transcript walked the command history instead: there is no way to tell those
arrows apart from the ones the keyboard sends, and the only fix is to turn
that translation off. The second is that a native drag runs across a *boxed
two-pane layout* — every line it copies carries a `│`, a column of side pane
and the padding in between. Neither is a thing this window can leave to the
terminal, so it owns both: `?1002` + `?1006` (SGR, because the old encoding
cannot express a column past 223), the wheel scrolls the transcript, and a
drag inside the transcript selects the *text* and copies it on release with
the frame stripped out. Shift-drag is still the terminal's own selection,
which every terminal that reports the mouse leaves alone, and `/mouse` hands
it back entirely. `/copy` still puts the last answer on the clipboard as
markdown.

Nothing in here knows about the vault. It is handed a callable that turns a
line of text into a `Response` and a few callables that report state; it draws
the result. `ui.py` still owns every colour and every glyph — this module owns
layout, input and the frame loop, and nothing else.

Stdlib only, like the rest of the package: `termios` and `tty` for cbreak
mode, and ANSI for everything drawn. No curses — curses wants to own colour
through its own attribute model, and every styled line here arrives from
`ui.markdown()` already carrying escapes.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field

from . import ui

# The side pane. 280px in the design; at a terminal's character grid that is
# about thirty columns, which is the narrowest a `BUDGET · TODAY` bar and a
# held-write path can both live in without wrapping into noise.
SIDE_W = 30
# Below this the split stops being a layout and becomes two cramped columns.
# The pane drops out and the transcript takes the whole width.
MIN_SPLIT = 76

# How much of a route the graph remembers. Ten is about a turn's worth of tool
# calls, and past four the brightness steps have run out anyway — the rest are
# there so a note read early in a long turn is still on the picture when the
# answer lands, which is the whole point of drawing a route rather than a dot.
TRAIL_MAX = 10
# What the aperture needs before the graph may have anything: label, meta,
# five rows of art, waveform, caption. Below this the aperture drops out on
# its own and there was never a question of fitting two things.
APERTURE_MIN = 9
# The graph's own floor and ceiling. Under five rows a neighbourhood is a
# smudge; over twelve it starts taking pane away from numbers you look up,
# and the picture is not improved by the extra rows — the nodes just get
# further apart.
GRAPH_MIN, GRAPH_MAX = 5, 12

# The commands that belong to the window rather than to V. They still obey the
# rule that a command is a shortcut for something you could have just said — a
# command with no spoken equivalent does not belong in the palette.
WINDOW_COMMANDS: list[tuple[str, str, str]] = [
    ("/mute", "stop listening — the aperture goes dark", "stop listening"),
    ("/ambient", "walk the aperture through every state",
     "show me every state you can be in"),
    ("/mouse", "hand the mouse back to the terminal", "stop grabbing my mouse"),
    ("/graph", "the whole vault, where the transcript is",
     "show me the vault"),
]

# Mouse reporting, as two strings so `/mouse` can toggle it at runtime.
#
# `?1002` is button-event tracking: press, release, and motion *while a button
# is down*. `?1003` would report every idle mouse move across the window, which
# is a repaint per pixel of travel for nothing this window needs. `?1006` is
# the SGR encoding, and it is not optional — the original X10 encoding packs a
# coordinate into one byte with a +32 bias, so it simply cannot say "column
# 224", and this window is routinely wider than that.
MOUSE_ON = "\x1b[?1002h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1002l\x1b[?1006l"

# Reverse video on and off. The selection is drawn by inverting cells rather
# than by painting a background colour, because the transcript already carries
# every colour in the palette and a background would have to win against all
# of them; inversion composes with whatever is underneath.
SELECT_ON = "\x1b[7m"
SELECT_OFF = "\x1b[27m"


def _slice_cols(plain: str, a: int, b: int) -> str:
    """Columns `a` up to `b` of an unstyled line.

    By column and not by index, because the vault is half Persian and a
    Python slice of a line with a ZWNJ or a wide glyph in it cuts somewhere
    other than where the selection was drawn.
    """
    out, col = [], 0
    for ch in plain:
        if col >= b:
            break
        if col >= a:
            out.append(ch)
        col += ui.cell_width(ch)
    return "".join(out)


def _highlight(styled: str, a: int, b: int) -> str:
    """Invert columns `a` up to `b` of a *styled* line, colours intact.

    The escapes are stepped over and charged no width, like `ui.clip()` does.
    The one subtlety is that a `RESET` inside the span turns the inversion off
    along with everything else — and the transcript is full of them, one at
    the end of every coloured run — so reverse video is re-asserted after
    every sequence that lands while the span is open.
    """
    out, col, i, on = [], 0, 0, False
    while i < len(styled):
        m = ui.ANSI_RE.match(styled, i)
        if m:
            out.append(m.group(0))
            if on:
                out.append(SELECT_ON)
            i = m.end()
            continue
        if not on and a <= col < b:
            out.append(SELECT_ON)
            on = True
        elif on and col >= b:
            out.append(SELECT_OFF)
            on = False
        out.append(styled[i])
        col += ui.cell_width(styled[i])
        i += 1
    return "".join(out) + (SELECT_OFF if on else "")


@dataclass
class Entry:
    """One thing in the transcript, stored as content rather than as pixels.

    Kept unrendered on purpose: a resize re-wraps the whole log to the new
    width, and a transcript stored as styled lines can only be re-wrapped by
    guessing where the escapes were.
    """

    kind: str                       # user · answer · cost · plain · proactive
    text: str = ""
    state: str = "speaking"
    confidence: float | None = None

    def render(self, cols: int) -> list[str]:
        # `markdown()` treats its `cols` as the width of the *text* and hangs
        # its indent outside that, so a pane of N columns has to ask for N
        # minus the indent. Handing it the pane width straight is how every
        # answer came out four columns long and walked through the border.
        body = max(20, cols - len(ui.HANG))
        if self.kind == "user":
            return [f"{ui.DIM2}›{ui.RESET} "
                    f"{ui.PAPER_HI}{ui.truncate(self.text, cols - 2)}{ui.RESET}"]
        if self.kind == "answer":
            return ui.answer(self.text, state=self.state,
                             confidence=self.confidence, cols=body).split("\n")
        if self.kind == "cost":
            return [self.text]
        if self.kind == "proactive":
            return ui.proactive(self.text, cols=body).split("\n")
        if self.kind == "raw":
            # Already styled by whoever built it — the command handlers print
            # their own views and are shared with the fallback prompt, so this
            # is where their output lands rather than being rebuilt twice.
            return self.text.split("\n")
        return ui.markdown(self.text, cols=body).split("\n")


@dataclass
class Panes:
    """What the side pane reports. Refreshed by the host after every turn."""

    turns: int = 0
    local_turns: int = 0
    spent: float = 0.0
    writes: int = 0
    daily_cap: float = 2.0
    proactive_used: int = 0
    proactive_cap: int = 6
    pending: list[tuple[str, float, str]] = field(default_factory=list)
    notes: int = 0
    model: str = ""
    online: bool = True


class Tui:
    """The window. `run()` blocks until the session ends."""

    def __init__(self, *, handle, commands, panes: Panes, version: str,
                 on_command=None, history: list[str] | None = None,
                 on_exit=None):
        self.handle = handle              # str -> (entries, panes) or None
        self.on_command = on_command      # str -> list[Entry] | None
        self.on_exit = on_exit
        self.commands = commands
        self.panes = panes
        self.version = version
        self.log: list[Entry] = []
        self.buf = ""
        self.cur = 0
        self.history: list[str] = list(history or [])
        self.hist_at: int | None = None
        self.scroll = 0                   # lines scrolled up from the bottom
        self.status = ""                  # the spinner line, while working
        self.busy = False
        # ---- the ambient state, which is what the aperture in the pane draws.
        # V has no microphone on this surface, so nothing here is pretending to
        # be one. The states are mapped onto the input that actually exists:
        # the first keystroke after silence is `wake`, a line being typed is
        # `listening`, a turn in flight is `thinking`, and the seconds after an
        # answer lands are `speaking`. Idle is the rest of the time, which is
        # most of it.
        self.muted = False
        self.tools = 1                    # rings that spin while thinking
        # …and now they mean something. `tools` was a constant 1 since the
        # aperture was written, because the engine finished a turn in one
        # opaque call and nothing could say what was happening inside it. It
        # is now the number of tool calls actually in flight (see `progress`),
        # which is what the arms were drawn to show.
        self._inflight = 0
        self._step = ""                   # the last thing the run did, by name
        # ---- the graph. `tools` says how *much* is happening; this says
        # *where*. A run names the note it is about in the argument of every
        # call it makes, so the window can point at that note on a picture of
        # the vault while the call is still out — which is the difference
        # between "reading a note" and "reading that one, next to the sprint".
        #
        # `_graph` is loaded once on a worker (see `_ensure_graph`) because a
        # cold layout is a couple of seconds and the first frame must not wait
        # for it. Until it arrives every one of these is inert and the pane
        # draws exactly what it drew before this existed.
        self._graph = None                # core.graph.Graph, once it lands
        self._graph_loading = False
        self._active = ""                 # the note this call is about
        # The route, most recent first. Not timestamped, on purpose: what is
        # drawn is the order the run actually visited notes in, at the speed it
        # actually visited them. There is no hold and no decay laid over the
        # top, so four reads in forty milliseconds show as four lit notes in
        # forty milliseconds.
        self._trail: list[str] = []
        self._view = None                 # the cached slice being drawn
        self._view_key: tuple = ()
        self.canvas = False               # `/graph` — the whole vault, big
        self._graph_box: tuple[int, int, int, int] | None = None
        self._canvas_box: tuple[int, int, int, int] | None = None
        self._graph_h = 0
        self._graph_off = 0
        self._stage_at = 0.0          # the aperture's own clock, in canvas mode
        # What each animated row was last painted with, so a repaint can skip
        # the rows that did not change. The camera moves most of them most of
        # the time — but the caption, the route under the canvas and the
        # aperture's own label are still, and at forty-five frames a second
        # the bytes they would cost are the ones that make this feel heavy
        # over a slow link.
        self._painted: dict[tuple[int, int], str] = {}
        self._typing_from = 0.0           # when the current line was started
        self._speak_until = 0.0
        self._unmuted_at = 0.0
        self._demo: list[str] | None = None   # `/ambient`, walking the states
        self._demo_at = 0.0
        # Keystroke energy, which is what drives the waveform in place of a mic
        # level. Decays on its own so a line left half-typed settles rather
        # than sitting at full amplitude forever.
        self._energy = 0.0
        self._energy_at = 0.0
        self._hscroll = 0                 # first column of the line on screen
        self._frame = -1
        # Where the ambient stage landed on screen last frame, as
        # (row, col, width, height) in 1-based terminal coordinates, or None
        # when the pane is too short to hold it. See `_repaint_stage()`.
        self._stage_box: tuple[int, int, int, int] | None = None
        self._stage_h = 0
        self._caret = 0                   # caret column inside the input field
        # ---- the mouse. `mouse` is whether this window is reporting it at
        # all; `_sel` is the drag in progress or the selection standing after
        # one, as ((row, col), (row, col)) in transcript-pane cells — pane
        # cells and not screen cells, so the arithmetic that turns a selection
        # back into text has nothing to do with where the border happens to
        # be. `_vis` is the styled transcript exactly as the last frame drew
        # it, which is the only thing a selection can be resolved against: the
        # log is stored unrendered, so there is no other record of which line
        # ended up on which row.
        self.mouse = True
        self._sel: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._dragging = False
        self._vis: list[str] = []
        self._vis_w = 0
        # A short-lived note in the title bar — "copied 4 lines" and nothing
        # heavier. It deliberately does not go in the transcript: the whole
        # point of the message is that you just selected something there, and
        # appending to the log would scroll the thing you selected off screen.
        self._flash = ""
        self._flash_until = 0.0
        self._fd = sys.stdin.fileno()
        self._saved = None
        self._pending = ""
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._resized = False
        self._cols, self._rows = 80, 24
        # The real terminal, captured once. Not `sys.stdout` at write time:
        # the command handlers are run under `redirect_stdout` so their views
        # can be caught and put in a pane, and that swap is global. A frame
        # written through it goes into the capture buffer instead of the
        # screen — and then gets replayed *inside* the pane, drawing the whole
        # window nested one frame deep in itself.
        self._out = sys.__stdout__ or sys.stdout
        # Three things draw: the input loop, the spinner thread, and SIGWINCH.
        # A frame is one absolute-positioned write of the whole screen, so two
        # of them interleaving does not tear a little — it nests one frame
        # inside another and the border ends up drawn twice across the middle.
        self._paint = __import__("threading").Lock()

    # ---- terminal ----------------------------------------------------
    def _enter(self) -> None:
        import termios
        import tty

        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        # Alternate screen, bracketed paste, and a blinking bar for the caret
        # in the design's violet — `[5 q` picks the shape, OSC 12 the colour.
        # Terminals that don't know either ignore them and keep their own
        # cursor, which is why neither is checked for first.
        #
        # Then `?1007l`, which is the whole of the scrolling bug. Alternate
        # scroll mode is on by default in VTE and most of its relatives, and
        # what it does is translate the wheel into arrow keys *whenever the
        # alternate screen is up* — on the theory that the program up there is
        # `less` and wants them. This one is not `less`, so every wheel notch
        # arrived as three `\x1b[A` and walked the command history. Nothing
        # downstream can tell those from the keyboard's own arrows; they are
        # byte-for-byte the same. So the translation is turned off at the
        # source, and the wheel is read as a wheel below.
        #
        # `?1007s` first, and `?1007r` on the way out: the mode belongs to the
        # terminal and outlives this process, so it is saved and put back
        # rather than assumed to have started on.
        self._out.write("\x1b[?1049h\x1b[?2004h\x1b[5 q\x1b]12;#7C5CFF\x07"
                        "\x1b[?1007s\x1b[?1007l"
                        + (MOUSE_ON if self.mouse else "")
                        + "\x1b[2J")
        self._out.flush()

    def _exit(self) -> None:
        import termios

        # `[0 q` puts the cursor shape back to whatever the terminal's own
        # setting is; OSC 112 does the same for its colour, and `?1007r` the
        # wheel. Leaving any of them changed is the kind of mess that outlives
        # the process. Mouse reporting is turned off unconditionally rather
        # than only when `self.mouse` is set: `/mouse` off writes `MOUSE_OFF`
        # itself, but a terminal that missed that write for any reason would
        # otherwise be left reporting clicks at a shell prompt as garbage.
        self._out.write(MOUSE_OFF + "\x1b[?1007r"
                        + "\x1b[?2004l\x1b[?25h\x1b[0 q\x1b]112\x07"
                        "\x1b[?1049l")
        self._out.flush()
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def _size(self) -> tuple[int, int]:
        c, r = shutil.get_terminal_size((80, 24))
        return max(40, c), max(12, r)

    # ---- drawing -----------------------------------------------------
    def _split(self) -> tuple[int, int]:
        """Transcript width and side width for the current terminal.

        The arithmetic is worth writing down, because getting it wrong by one
        does not look like an off-by-one — every row is a column too long, the
        terminal wraps it, and the whole window appears to shift two columns
        right and draw itself twice.

            with a pane:  │ ␣ text ␣ │ ␣ side ␣ │   →  tw + sw + 7
            without:      │ ␣ text ␣ │             →  tw + 4
        """
        if self._cols < MIN_SPLIT:
            return self._cols - 4, 0
        return self._cols - SIDE_W - 7, SIDE_W

    def _titlebar(self) -> str:
        p = self.panes
        dots = f"{ui.LINE_3}●●●{ui.RESET}"
        name = (f"{ui.DIM2}v — repl — {self._cols}×{self._rows}{ui.RESET}")
        if self._flash and time.time() >= self._flash_until:
            self._flash = ""
        right = " · ".join(filter(None, [
            f"{ui.SEED}{self._flash}{ui.RESET}" if self._flash else "",
            f"{ui.DIM2}{ui.G_ROUTE} {ui.BODY_HI}{p.model}{ui.RESET}"
            if p.model else "",
            f"{ui.FREE}{ui.G_LOCAL} online{ui.RESET}" if p.online
            else f"{ui.GREY}{ui.G_LOCAL} local{ui.RESET}",
            f"{ui.BODY_HI}${p.spent:.2f}{ui.RESET}",
        ]))
        left = f"{dots} {name}"
        # `╭─ ` + this + ` ─╮` has to come to exactly the window width.
        gap = (self._cols - 6 - ui.cell_width(left) - ui.cell_width(right))
        return f"{left}{' ' * max(1, gap)}{right}"

    # ---- the ambient state -------------------------------------------
    def ambient(self) -> str:
        """Which of the six states V is in right now.

        Read rather than set, and read from things that are actually true —
        there is no state here that some other part of the program has to
        remember to clear, which is how the light and the program drift apart.
        """
        now = time.time()
        if self.muted:
            return "muted"
        if self._demo:
            return self._demo[0]
        if self.busy:
            return "thinking"
        if now < self._speak_until:
            return "speaking"
        if self.buf:
            # The spec gives wake its own state because it is the moment V
            # starts paying attention, and it is over almost immediately.
            return "wake" if now - self._typing_from < 0.55 else "listening"
        return "idle"

    def _motion(self) -> bool:
        """Reduced motion. Every loop stops; colour and size still change."""
        return not os.environ.get("V_NO_ANIM")

    def _intensity(self) -> float:
        """0 → 1 over the 800ms after an unmute.

        The spec is asymmetric on purpose: 0ms into muted, 800ms out of it.
        Going dark has to be instant or you cannot trust it; coming back is
        allowed to be a fade, because nothing is at stake in that direction.
        """
        if self.muted or not self._unmuted_at or not self._motion():
            return 1.0
        return max(0.0, min(1.0, (time.time() - self._unmuted_at) / 0.8))

    def _energy_now(self) -> float:
        """Typing amplitude, decayed. The stand-in for a mic level."""
        if not self._energy_at:
            return 0.0
        return self._energy * max(0.0, 1.0 - (time.time() - self._energy_at) / 1.2)

    def _tick_demo(self) -> None:
        now = time.time()
        if self._demo and now - self._demo_at >= 2.6:
            self._demo, self._demo_at = self._demo[1:], now
            if not self._demo:
                self._demo = None

    # ---- the graph ---------------------------------------------------
    def _ensure_graph(self) -> None:
        """Start loading the vault's shape, once, on a worker.

        A cold layout is a relaxation over every note in the vault — a couple
        of seconds, and then cached on disk for as long as the links hold
        still. That is nothing to pay once and far too much to pay before the
        first frame, so it happens off to the side and the pane simply has no
        graph in it until it lands. Nothing waits on this and nothing fails if
        it never finishes.
        """
        # Retried on a later turn if it came back empty — a vault with no index
        # yet gets one eventually, and `load()` on a database that is not there
        # returns immediately, so asking again costs nothing. Only a load that
        # *succeeded* is expensive, and that one is not asked again.
        if self._graph or self._graph_loading:
            return
        self._graph_loading = True

        def work() -> None:
            try:
                from . import graph as _graph
                loaded = _graph.load()
            except Exception:
                loaded = None                 # a decoration must not raise
            self._graph = loaded if loaded else False
            self._graph_loading = False

        __import__("threading").Thread(target=work, daemon=True).start()

    def _touch(self, candidate: str, focus: bool = True) -> None:
        """Note that the run just reached for something. Cheap enough to call
        from the reader thread, which is where it is called from.

        Resolution is a dict lookup — `shells` hands over path-shaped strings
        and this is the end that knows which of them are notes. Anything that
        does not resolve is dropped without comment: the run reads plenty of
        files that are not vault notes, and a graph is not the place to
        mention them.

        No lock, for the same reason `progress` takes none. The renderer can
        catch this list between the remove and the insert and draw one frame
        without that note on it, which is a sixtieth of a second in which a
        note V has read twice is briefly drawn as one it has read once.
        """
        g = self._graph
        if not g:
            return
        rel = g.resolve(candidate)
        if not rel:
            return
        if rel in self._trail:
            self._trail.remove(rel)
        self._trail.insert(0, rel)
        del self._trail[TRAIL_MAX:]
        if focus:
            self._active = rel

    def _slice(self):
        """The piece of graph to draw right now, rebuilt only when it moves.

        Keyed on the focus rather than on the clock. The frame loop asks for
        this thirty times a second and the answer changes a handful of times a
        turn, so the relaxation behind a neighbourhood runs when a tool call
        lands and never on a frame.
        """
        g = self._graph
        if not g:
            return None
        if self.canvas:
            key = ("whole", len(g.nodes))
            if key != self._view_key:
                self._view, self._view_key = g.whole(), key
            return self._view
        focus = [r for r in ([self._active] if self._active else [])
                 + self._trail][:4]
        if not focus:
            # At rest it is the whole vault, not an empty box. The pane keeps
            # a graph in it the way it keeps the aperture in it — the picture
            # is the point, and a vault has a silhouette you learn even at
            # twenty-six columns, with the hubs lit through the middle of it.
            key = ("whole", len(g.nodes))
            if key != self._view_key:
                self._view, self._view_key = g.whole(), key
            return self._view
        key = ("near", tuple(focus))
        if key != self._view_key:
            self._view = g.neighbourhood(focus, hops=1, limit=18)
            self._view_key = key
        return self._view

    def _graph_rows(self, budget: int) -> list[str]:
        """The side pane's graph block, or nothing if it does not fit.

        Takes a height and nothing else, exactly as `_stage` does. The width is
        the pane's own and is not a parameter on purpose: the partial repaint
        knows the *box*, which is two columns wider than the content inside it,
        and handing that number in drew a thirty-column picture into a
        twenty-eight-column slot every frame the rectangle repainted.

        The aperture has first claim on the pane. It is V's own mark and the
        one thing in the window you read from across the room, so the graph
        takes what is left over after the aperture has its nine rows — and on
        a terminal too short for both, the graph is what goes.

        The slot is held whether or not anything has been read yet. Sizing it
        to the route would mean the aperture growing and shrinking every time
        V picked up a note, and a pane that reflows while you are reading it is
        worse than one row of vault at rest.
        """
        w = SIDE_W - 2
        room = budget - APERTURE_MIN - 1          # -1 for the blank between
        if room < GRAPH_MIN + 2:                  # +2 for label and caption
            return []
        if self.canvas:
            # The same picture is already on screen at full size. Drawing it
            # twice is not just redundant: the two want different slices, and
            # `_slice` holds one, so they would take turns invalidating each
            # other's cache and re-run the relaxation on every frame.
            return []
        view = self._slice()
        if view is None or not view.nodes:
            return []
        art = min(room - 2, GRAPH_MAX)            # -2 for the label and caption
        here = self._active or (self._trail[0] if self._trail else "")
        rows = [f"{ui.DIM1}{' '.join('GRAPH')}{ui.RESET}"]
        rows += ui.graph(view, active=self._active, lit=self._trail,
                         w=w, h=art, t=time.time(), motion=self._motion())
        # Blank at rest rather than a label for the whole vault: the footer
        # already counts the notes, and this line has one job, which is to name
        # the note the mark is sitting on.
        rows.append(ui.graph_caption(here, w))
        return rows

    def _canvas(self, cols: int, height: int) -> list[str]:
        """`/graph`. The whole vault where the transcript usually is.

        The transcript is hidden rather than shrunk, because a graph split down
        the middle of an eighty-column terminal is two bad panes instead of one
        good one. What the transcript was carrying that still matters mid-run
        is the route, so the route is printed under the picture — this mode
        stays usable while a turn is in flight, which is the only time anybody
        would want it.
        """
        g = self._graph
        if not g:
            return [""] * height
        view = self._slice()
        if view is None or not view.nodes:
            return [""] * height
        # The route takes a line per note and a blank above it, and the picture
        # takes the rest. Capped, so a long route cannot eat the whole canvas.
        route = self._trail[:min(5, max(0, (height - GRAPH_MIN - 2) // 2))]
        art = height - len(route) - (2 if route else 1)
        out = [f"{ui.DIM1}{' '.join('VAULT')}{ui.RESET}  "
               f"{ui.DIM3}{len(g.nodes)} notes · {len(g.edges)} links · "
               f"/graph to close{ui.RESET}"]
        out += ui.graph(view, active=self._active, lit=self._trail,
                        w=cols, h=max(3, art), t=time.time(),
                        motion=self._motion())
        if route:
            out.append("")
            for i, rel in enumerate(route):
                mark = ui.CORE_HI if rel == self._active else (
                    ui.LIFT if i < 2 else ui.SEED if i < 4 else ui.DEEP)
                out.append(f"{mark}●{ui.RESET} "
                           f"{ui.graph_caption(rel, cols - 2)}")
        while len(out) < height:
            out.append("")
        return out[:height]

    def _stage(self, height: int) -> list[str]:
        """The aperture, its waveform and its caption. Exactly `height` lines.

        Given whatever the blocks below it did not want. Below five rows there
        is no circle left to draw — a two-row aperture is a smear, not a mark —
        so it drops out entirely rather than degrading into noise.
        """
        w = SIDE_W - 2
        state = self.ambient()
        spec = ui.AMBIENT[state]
        motion = self._motion()
        t = time.time()

        art_h = height - 4                        # label, meta, wave, caption
        if art_h < 5:
            return []
        out = [f"{ui.DIM1}{ui.truncate(' '.join(spec['label']), w)}{ui.RESET}",
               f"{ui.DIM3}{ui.truncate(spec['meta'], w)}{ui.RESET}"]
        out += ui.aperture(state, t, w, art_h, tools=self.tools,
                           motion=motion, intensity=self._intensity())
        out.append(ui.waveform(spec["wave"], t, w,
                               energy=self._energy_now() or 0.35,
                               motion=motion))
        # While he is listening the caption is the transcript — and on this
        # surface the transcript is the line you are typing. Same rule as the
        # device: what is shown under the mark is what he currently has.
        live = state == "listening" and bool(self.buf)
        cap = self.buf if live else spec["caption"]
        out.append(f"{ui.PAPER if live else ui.MUTED}"
                   f"{ui.truncate(cap, w)}{ui.RESET}")
        while len(out) < height:
            out.append("")
        return out

    def _side(self, height: int) -> list[str]:
        """The aperture, then SESSION · BUDGET · PENDING WRITES.

        A live region: it never scrolls with the log, and everything in it is
        current rather than historical. The aperture takes the top because it
        is the one thing in here you read at a glance from across the room.
        """
        p = self.panes
        w = SIDE_W - 2
        out: list[str] = []

        def block(title: str, rows: list[str]) -> None:
            if out:
                out.append("")
            out.append(f"{ui.DIM1}{' '.join(title)}{ui.RESET}")
            out.extend(rows)

        def row(k: str, v: str, tint: str = ui.BODY_HI) -> str:
            return (f"{ui.DIM2}{ui.pad(k, w - ui.cell_width(v))}{ui.RESET}"
                    f"{tint}{v}{ui.RESET}")

        block("SESSION", [
            row("turns", str(p.turns)),
            row("local", f"{p.local_turns} / {p.turns}", ui.FREE),
            row("writes", str(p.writes)),
        ])
        frac = (p.spent / p.daily_cap) if p.daily_cap else 0.0
        block("BUDGET · TODAY", [
            ui.bar(frac, w),
            f"{ui.DIM3}${p.spent:.2f} of ${p.daily_cap:.2f}{ui.RESET}",
            f"{ui.DIM3}{p.proactive_cap - p.proactive_used} proactive left"
            f"{ui.RESET}",
            ui.pips(p.proactive_used, p.proactive_cap),
        ])
        if p.pending:
            rows = []
            for path, conf, status in p.pending[:4]:
                tint = ui.conf_colour(conf)
                rows.append(f"{tint}▎{ui.RESET}{ui.PAPER}"
                            f"{ui.truncate(path, w - 1)}{ui.RESET}")
                rows.append(f" {ui.DIM3}{ui.G_CONF} {conf:.2f} · {status}"
                            f"{ui.RESET}")
            block("PENDING WRITES", rows)

        # The blocks are the pane's floor: they are numbers you look up, and
        # they must not move as the aperture breathes. So they are measured
        # first and what is left over is divided between the aperture and the
        # graph — in that order, the aperture first. On a short terminal that
        # leftover is nothing and neither one is there.
        budget = max(0, height - 1 - len(out) - 1)
        picture = self._graph_rows(budget)
        stage = self._stage(budget - (len(picture) + 1 if picture else 0))
        if not stage:
            # `_graph_rows` measures against the aperture's floor, so this is
            # not reachable by arithmetic — but the rule it encodes is the one
            # that matters: a pane with no room for the mark has no room for a
            # picture either, and the graph is what goes.
            picture = []
        blocks, out = out, []
        if stage:
            out += stage + [""]
        if picture:
            out += picture + [""]
        out += blocks
        out = out[:max(0, height - 1)]
        # Remembered so the frame loop can repaint them on its own — measured
        # after the truncation above, because a pane one row short clips the
        # stage and the partial repaint must not write past it.
        self._stage_h = min(len(stage), max(0, height - 1))
        self._graph_off = len(stage) + 1 if stage else 0
        self._graph_h = max(0, min(len(picture),
                                   max(0, height - 1) - self._graph_off))
        # The footer sits on the last line of the pane rather than after the
        # blocks, so it does not drift up and down as the session grows.
        while len(out) < height - 1:
            out.append("")
        out.append(f"{ui.DIM3}{ui.truncate(f'{p.notes} notes', w)}{ui.RESET}")
        return out

    def _transcript(self, cols: int, height: int) -> list[str]:
        lines: list[str] = []
        for e in self.log:
            lines.extend(e.render(cols))
        if self.status:
            lines.append(self.status)
        # `scroll` counts lines held back from the bottom, so a new answer
        # always lands in view unless you have deliberately scrolled away.
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - height)))
        end = len(lines) - self.scroll
        window = lines[max(0, end - height):end]
        return [""] * (height - len(window)) + window

    def _palette_rows(self, cols: int) -> list[str]:
        """The command list, shown while the line being typed starts with `/`.

        Live, on every keystroke, which is the thing the old readline prompt
        could not do — it could only answer Tab, and only with a column of
        bare names.
        """
        if not self.buf.startswith("/"):
            return []
        typed = self.buf.split(" ")[0]
        body = ui.palette(self.commands, typed, cols=cols)
        return body.split("\n")[:8]

    def draw(self) -> None:
        self._cols, self._rows = self._size()
        tw, sw = self._split()
        body_h = self._rows - 4
        out = ["\x1b[H"]

        def line(text: str) -> None:
            # `\n` only: cbreak leaves ONLCR on, so the terminal supplies the
            # carriage return. Writing `\r\n` here sends two of them.
            out.append(text + "\x1b[K\n")

        # top border, with the title bar inside it
        line(f"{ui.LINE_2}╭─{ui.RESET} {self._titlebar()} "
             f"{ui.LINE_2}─╮{ui.RESET}")

        left = (self._canvas(tw, body_h) if self.canvas
                else self._transcript(tw, body_h))
        self._stage_h = self._graph_h = 0
        right = self._side(body_h) if sw else []
        # The pane's first row is screen row 2 — row 1 is the top border — and
        # its first column is `│ text │ ` wide, so tw + 6 on a 1-based screen.
        # The stage sits at the top of the pane, so that is where it starts;
        # the graph sits directly under it, at whatever offset `_side` just
        # measured.
        self._stage_box = ((2, tw + 6, sw, self._stage_h)
                           if sw and self._stage_h else None)
        self._graph_box = ((2 + self._graph_off, tw + 6, sw, self._graph_h)
                           if sw and self._graph_h else None)
        palette = self._palette_rows(tw)
        if palette:
            # Drawn over the foot of the transcript rather than pushed in
            # above the prompt: the prompt must not move while you are typing
            # into it.
            left = left[:max(0, len(left) - len(palette))] + palette
        # The canvas is a rectangle too — the transcript pane, whole. Without
        # this, every frame of `/graph` rewrote all hundred and eighteen
        # columns of the window to move a dozen particles, which is the exact
        # cost `_repaint_stage` exists to avoid; it just happened to be a
        # bigger rectangle than the one that was thought of first. Column 3
        # because the row opens `│ `. Not claimed while the palette is over
        # the foot of it, or while a drag is being resolved against it.
        self._canvas_box = ((2, 3, tw, body_h)
                            if self.canvas and not palette and not self._sel
                            else None)
        # The screen is about to be written whole, so nothing that was there
        # before can be skipped as unchanged.
        self._painted.clear()
        # The frame the mouse will be resolved against. `_transcript()` pads
        # to exactly `body_h`, so pane row *i* is `left[i]` and nothing has to
        # be recomputed at click time — which matters, because by then the
        # transcript may have been re-wrapped by a resize and the row the
        # button went down on would no longer hold the same text.
        self._vis, self._vis_w = left, tw
        sel = self._sel_rows() if self.mouse else {}

        for i in range(body_h):
            # Clipped unconditionally. Content is rendered to the pane width,
            # but a handler that measured something else — or a `raw` view
            # built elsewhere — must not be able to break the frame.
            lt = ui.pad(ui.clip(left[i] if i < len(left) else "", tw), tw)
            if i in sel:
                # After the pad, so a selection that runs past the end of a
                # short line lights the rest of the row rather than stopping
                # at the last character — the shape of the drag is the thing
                # you are reading while you make it.
                lt = _highlight(lt, *sel[i])
            if sw:
                rt = ui.pad(ui.clip(right[i] if i < len(right) else "", sw), sw)
                line(f"{ui.LINE_2}│{ui.RESET} {lt} {ui.LINE_2}│{ui.RESET} {rt} "
                     f"{ui.LINE_2}│{ui.RESET}")
            else:
                line(f"{ui.LINE_2}│{ui.RESET} {lt} {ui.LINE_2}│{ui.RESET}")

        # the divider above the prompt
        if sw:
            line(f"{ui.LINE_2}├{'─' * (tw + 2)}┴{'─' * (sw + 2)}┤{ui.RESET}")
        else:
            line(f"{ui.LINE_2}├{'─' * (tw + 2)}┤{ui.RESET}")

        # the prompt
        hint = f"{ui.DIM3}⏎{ui.RESET}"
        # `│ › ` before the text and `⏎ │` after it: the text has the window
        # minus seven columns, and the caret sits at column 5 on a 1-based
        # screen. Getting either off by one puts the caret under the `›` or
        # pushes the last character through the border.
        avail = max(8, self._cols - 7)
        shown, caret = self._field(avail)
        self._caret = caret
        if self.busy:
            body = f"{ui.DIM3}›{ui.RESET} {ui.DIM3}working…{ui.RESET}"
        elif shown:
            body = f"{ui.SEED}›{ui.RESET} {ui.PAPER_HI}{shown}{ui.RESET}"
        else:
            # No log, no greeting, no tour — but the one thing worth saying
            # here is how to get at the commands, and it costs nothing to say
            # it in the space the line is not using yet.
            body = (f"{ui.SEED}›{ui.RESET} "
                    f"{ui.DIM3}ask, or / for commands{ui.RESET}")
        line(f"{ui.LINE_2}│{ui.RESET} {ui.pad(body, self._cols - 5)}{hint} "
             f"{ui.LINE_2}│{ui.RESET}")
        out.append(f"{ui.LINE_2}╰{'─' * (self._cols - 2)}╯{ui.RESET}\x1b[K")

        # Put the real cursor where you are typing and turn it back on. It is
        # hidden for the duration of the paint and shown again here, which is
        # the whole trick: a visible cursor during a full-screen write darts
        # along every line as it lands, and a cursor that is never shown at all
        # leaves you typing into a field with nothing marking your place.
        out.append(f"\x1b[{self._rows - 1};{5 + caret}H")
        if not self.busy:
            out.append("\x1b[?25h")
        with self._paint:
            self._out.write("\x1b[?25l" + "".join(out))
            self._out.flush()

    def _repaint_stage(self) -> bool:
        """Repaint the ambient stage alone, in place. False if it can't be.

        A whole frame re-wraps every entry in the transcript through
        `ui.markdown()` and rewrites the entire screen, which is the wrong
        price to pay thirty times a second to move a dozen particles — it is
        also *why* the aperture used to run at eight frames: the frame rate
        was pinned to the cost of the most expensive thing on screen. The
        stage is a fixed rectangle inside the side pane and nothing else in
        the window is animating, so it can be addressed on its own.

        The rectangles come from the last full frame. If there hasn't been
        one, or the window changed size since, the caller falls back to
        `draw()` — this never guesses at a layout it did not itself measure.

        Three of them now, and the third is the reason this comment is longer
        than it was: `/graph` puts a moving picture where the *transcript* is,
        and repainting that through `draw()` cost five milliseconds and thirteen
        kilobytes a frame to move a dozen particles — the very thing this
        method was written to stop doing, dodged only because the canvas
        happened to be a bigger rectangle than the one first thought of.
        """
        if (self._cols, self._rows) != self._size():
            return False
        # Every block is re-rendered whole and then checked against the height
        # it had last frame. One that changed size means the route grew past
        # what the pane was holding, or the pane moved — either way that is a
        # full frame's business and not a rectangle's.
        boxes = []
        now = time.time()
        # The aperture keeps its own clock while the canvas is up. It is a
        # breathing pattern that reads the same at eighteen frames as at
        # forty-five — and in this mode it is in the corner of a window whose
        # whole left half is a moving graph, which is to say nobody is looking
        # at it. Rendering it anyway was a third of the frame.
        if self._stage_box and (not self.canvas
                                or now - self._stage_at >= 1 / 18):
            self._stage_at = now
            boxes.append((self._stage_box, self._stage(self._stage_box[3])))
        if self._graph_box:
            # `_graph_rows` takes the *stage's* budget and subtracts the
            # aperture's floor from it, so the height it was given last frame
            # has to be handed back the same way round.
            boxes.append((self._graph_box,
                          self._graph_rows(self._graph_box[3]
                                           + APERTURE_MIN + 1)))
        if self.canvas:
            if not self._canvas_box:
                # A palette over the foot of it, or a drag being resolved
                # against it. Both are the transcript pane's business.
                return False
            _, _, cw, ch = self._canvas_box
            boxes.append((self._canvas_box, self._canvas(cw, ch)))
        if not boxes:
            return False
        # Rendered before anything is written, so a block that stopped fitting
        # backs out without having half-painted the screen on its way.
        painted = []
        for box, rows in boxes:
            row0, col0, w, h = box
            if rows is None or len(rows) != h:   # it stopped fitting
                return False
            for i, text in enumerate(rows):
                painted.append((row0 + i, col0, ui.pad(ui.clip(text, w), w)))

        out = ["\x1b[?25l"]
        seen = self._painted
        for row, col0, text in painted:
            # The camera moves most rows most of the time, but not all of them:
            # a caption, the route under the canvas, the aperture's own label.
            # Skipping those is the difference between a quiet link and a
            # window that feels like it is being streamed to you.
            if seen.get((row, col0)) == text:
                continue
            seen[(row, col0)] = text
            out.append(f"\x1b[{row};{col0}H{text}")
        if len(out) == 1:
            return True                      # nothing moved; nothing to say
        # Back to the input line. Same rule as a full frame: the cursor is
        # hidden for the write and put back where you are typing after it.
        out.append(f"\x1b[{self._rows - 1};{5 + self._caret}H")
        if not self.busy:
            out.append("\x1b[?25h")
        with self._paint:
            self._out.write("".join(out))
            self._out.flush()
        return True

    def _field(self, avail: int) -> tuple[str, int]:
        """The visible slice of the input line, and the caret's column in it.

        A line longer than the field scrolls sideways, and the window is
        *remembered* rather than recomputed from scratch each frame — pinning
        it to the end of the buffer is what made the caret sit under the wrong
        character the moment you pressed left-arrow on a long line.
        """
        buf, cur = self.buf, self.cur
        if ui.cell_width(buf) <= avail:
            self._hscroll = 0
            return buf, ui.cell_width(buf[:cur])
        off = min(self._hscroll, len(buf) - 1)
        off = min(off, cur)                       # caret walked off the left
        # …and off the right: widen the window from the left until it fits.
        while ui.cell_width(buf[off:cur]) > avail - 1:
            off += 1
        self._hscroll = off
        shown, w = [], 0
        for ch in buf[off:]:
            cw = ui.cell_width(ch)
            if w + cw > avail:
                break
            shown.append(ch)
            w += cw
        return "".join(shown), ui.cell_width(buf[off:cur])

    # ---- the mouse ---------------------------------------------------
    def _pane(self) -> tuple[int, int, int, int]:
        """The transcript's text area on screen, 1-based, as (row, col, w, h).

        Row 1 is the top border, so the pane starts at row 2. Its first text
        column is three: `│`, a space, then text. Both come straight from the
        frame written in `draw()`, and if that layout changes this has to
        change with it — a selection resolved against the wrong origin does
        not look like an off-by-one either, it copies the line above.
        """
        return 2, 3, self._vis_w, len(self._vis)

    def _clear_selection(self) -> None:
        self._sel, self._dragging = None, False

    def _sel_rows(self) -> dict[int, tuple[int, int]]:
        """The selected column span per pane row, as {row: (start, end)}."""
        if not self._sel:
            return {}
        (r0, c0), (r1, c1) = sorted(self._sel)
        rows = {}
        for r in range(max(0, r0), min(r1, len(self._vis) - 1) + 1):
            a = c0 if r == r0 else 0
            b = c1 if r == r1 else self._vis_w
            if b > a:
                rows[r] = (a, b)
        return rows

    def _selection_text(self) -> str:
        """What is selected, as plain text with the window taken off.

        This is the reason the window reports the mouse at all. A native drag
        across this layout copies `│ ` and a strip of side pane on every line,
        because the terminal is selecting a rectangle of *screen* and the
        screen is a box. This selects a rectangle of transcript instead, and
        the transcript is the only part of the frame anyone wants.
        """
        out = [_slice_cols(ui.strip_ansi(self._vis[r]), a, b).rstrip()
               for r, (a, b) in sorted(self._sel_rows().items())]
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(out)

    def _copy_selection(self) -> None:
        text = self._selection_text()
        if not text:
            self._clear_selection()
            return
        # The selection stays lit after the button comes up, the way it does
        # in a terminal — it is cleared by the next keystroke, not by letting
        # go. `primary` as well as the clipboard so middle-click paste still
        # behaves, which is otherwise the thing you lose by taking the mouse.
        self._dragging = False
        how = ui.copy(text, primary=True)
        n = text.count("\n") + 1
        self._flash_note(f"copied {n} line{'' if n == 1 else 's'}" if how
                         else "no clipboard tool — install wl-clipboard")

    def _flash_note(self, text: str, secs: float = 2.5) -> None:
        self._flash, self._flash_until = text, time.time() + secs

    def _mouse(self, key: str) -> bool:
        """One SGR mouse report. False if `key` was not one.

        The encoding: `\x1b[<b;col;rowM` for a press or a drag and `…m` for a
        release, both 1-based. In `b`, the low two bits are the button, 32 is
        "this is motion, not a press", and 64 is the wheel — which is reported
        as a button that is only ever pressed, never released.
        """
        m = self._MOUSE.fullmatch(key)
        if not m:
            return False
        if not self.mouse:
            # `/mouse` off. The terminal has been told to stop reporting, so
            # this is at most a report already in flight when it was — eaten
            # rather than answered, and certainly not typed into the line.
            return True
        b, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        row0, col0, w, h = self._pane()
        if b & 64:                                  # wheel: 64 up, 65 down
            # Three lines a notch, which is what the terminal's own alternate
            # scroll would have sent — the same feel, now landing on the
            # transcript instead of on the command history.
            self.scroll = max(0, self.scroll + (-3 if b & 1 else 3))
            self._clear_selection()
            return True
        if b & 0b11:                                # middle, right: not ours
            return True
        r, c = y - row0, x - col0
        if m.group(4) == "m":                       # release
            if self._dragging:
                self._copy_selection()
            return True
        if b & 32:                                  # drag
            if self._dragging and self._sel:
                # Clamped rather than dropped: a drag that runs off the edge
                # of the pane is a drag that means "to the end of the line",
                # which is what everything else with a selection does.
                self._sel = (self._sel[0],
                             (max(0, min(h - 1, r)), max(0, min(w, c))))
            return True
        if 0 <= r < h and 0 <= c < w:               # press, inside
            self._sel, self._dragging = ((r, c), (r, c)), True
        else:
            self._clear_selection()
        return True

    # ---- input -------------------------------------------------------
    # A CSI sequence, an SS3 sequence, or a bare Esc followed by one character.
    # `<` is in the parameter class for the SGR mouse reports (`\x1b[<0;40;7M`)
    # — without it the whole report failed to match, the leading Esc came back
    # as a lone key, and the rest of it was typed into the input line.
    _SEQ = re.compile(r"\x1b(?:\[[0-9;?<>]*[A-Za-z~]|O[A-Za-z]|.)")
    _MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def _fill(self, timeout: float | None) -> bool:
        """Pull whatever the terminal has into the pending buffer.

        `os.read` on the raw descriptor rather than `sys.stdin.read(1)`, and
        this is not a style preference — it is the bug that ate every keystroke
        after the first. `sys.stdin` is buffered, so reading one character from
        it pulls the whole burst out of the kernel and parks the rest in
        Python's buffer, where `select` cannot see it. Paste a line, or type
        fast enough that `/tasks⏎` arrives as one read, and the terminal goes
        deaf after the `/` — select keeps reporting nothing to read while six
        characters and a carriage return sit in a buffer nobody drains.
        """
        import select

        if self._pending:
            return True
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(self._fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        # Incremental, because a Persian character is two or three bytes and a
        # 4096-byte read can land in the middle of one.
        self._pending += self._dec.decode(data)
        return bool(self._pending)

    def _take(self) -> str | None:
        """One key token off the front of the buffer, if a whole one is there."""
        p = self._pending
        if not p:
            return None
        if p[0] != "\x1b":
            self._pending = p[1:]
            return p[0]
        m = self._SEQ.match(p)
        if not m:
            return None                 # a partial sequence; wait for the rest
        self._pending = p[m.end():]
        return m.group(0)

    def _read(self, timeout: float | None = None) -> str | None:
        """One key, as a token. None on timeout."""
        while True:
            tok = self._take()
            if tok is not None:
                return tok
            # A lone Esc is a partial sequence that will never complete, so a
            # short second wait distinguishes "the rest is coming" from "that
            # was the Escape key".
            if not self._fill(timeout if not self._pending else 0.03):
                if self._pending:
                    tok, self._pending = self._pending[0], self._pending[1:]
                    return tok
                return None

    def _paste(self) -> str:
        """Everything up to the bracketed-paste terminator, as one insert."""
        end = "\x1b[201~"
        while end not in self._pending:
            if not self._fill(0.5):
                break
        body, _, rest = self._pending.partition(end)
        self._pending = rest
        return body.replace("\n", " ").replace("\r", " ")

    def _beat(self, weight: float = 1.0) -> None:
        """Register a keystroke as amplitude for the waveform.

        This surface has no microphone, and the spec is explicit that the bars
        must be driven by a real level rather than a replayed loop. So they are
        driven by the real input this surface *does* have: how fast you are
        typing. Fast keys ride high, a pause decays.
        """
        now = time.time()
        gap = now - (self._energy_at or now)
        self._energy = min(1.0, self._energy_now() * 0.6
                           + weight * (0.6 if gap < 0.22 else 0.38))
        self._energy_at = now

    def _edit(self, key: str) -> str | None:
        """Apply one key to the input line. Returns a submitted line, or None."""
        b, c = self.buf, self.cur
        if not b and key not in ("\r", "\n"):
            self._typing_from = time.time()
            self._demo = None           # you typing outranks the walkthrough
        if key in ("\r", "\n"):
            self.buf, self.cur, self.hist_at = "", 0, None
            return b
        if key == "\x7f":                                   # backspace
            if c:
                self.buf, self.cur = b[:c - 1] + b[c:], c - 1
        elif key == "\x01":                                 # ctrl-a
            self.cur = 0
        elif key == "\x05":                                 # ctrl-e
            self.cur = len(b)
        elif key == "\x15":                                 # ctrl-u
            self.buf, self.cur = b[c:], 0
        elif key == "\x0b":                                 # ctrl-k
            self.buf = b[:c]
        elif key == "\x17":                                 # ctrl-w
            head = b[:c].rstrip()
            cut = head.rfind(" ") + 1
            self.buf, self.cur = b[:cut] + b[c:], cut
        elif key in ("\x1b[D", "\x02"):                     # ← · ctrl-b
            self.cur = max(0, c - 1)
        elif key in ("\x1b[C", "\x06"):                     # → · ctrl-f
            self.cur = min(len(b), c + 1)
        elif key in ("\x1bb", "\x1b[1;5D", "\x1b[1;3D"):    # word left
            self.cur = b[:c].rstrip().rfind(" ") + 1
        elif key in ("\x1bf", "\x1b[1;5C", "\x1b[1;3C"):    # word right
            nxt = b.find(" ", c + (len(b[c:]) - len(b[c:].lstrip())))
            self.cur = len(b) if nxt < 0 else nxt
        elif key in ("\x1b[H", "\x1b[1~"):
            self.cur = 0
        elif key in ("\x1b[F", "\x1b[4~"):
            self.cur = len(b)
        elif key in ("\x1b[A", "\x1b[B"):                   # history
            if self.history:
                at = len(self.history) if self.hist_at is None else self.hist_at
                at += -1 if key == "\x1b[A" else 1
                at = max(0, min(len(self.history), at))
                self.hist_at = at
                self.buf = self.history[at] if at < len(self.history) else ""
                self.cur = len(self.buf)
        elif key == "\x1b[5~":                              # page up
            self.scroll += self._rows - 6
        elif key == "\x1b[6~":
            self.scroll = max(0, self.scroll - (self._rows - 6))
        elif key == "\t":
            hits = [c_ for c_, _, _ in self.commands if c_.startswith(b)]
            if len(hits) == 1:
                self.buf, self.cur = hits[0] + " ", len(hits[0]) + 1
        elif key == "\x1b[200~":
            paste = self._paste()
            self.buf, self.cur = b[:c] + paste + b[c:], c + len(paste)
            self._beat()
        elif key >= " " and not key.startswith("\x1b"):
            self.buf, self.cur = b[:c] + key + b[c:], c + len(key)
            self._beat()
        return None

    # ---- the loop ----------------------------------------------------
    def say(self, entry: Entry) -> None:
        self.log.append(entry)
        self.scroll = 0
        # The rows the selection was measured against have just moved out from
        # under it. Terminals drop a selection for the same reason.
        self._clear_selection()

    def _animating(self) -> bool:
        """Whether a repaint would actually change anything.

        Muted has no animation at all, and that is the point of it — a state
        no software path can produce has to look like one, so it does not get
        a frame clock either.
        """
        if not self._motion():
            return False
        if self._demo:
            return True
        if self._graph and (self._graph_h or self.canvas):
            # Muted silences the aperture, and rightly — a state no software
            # path can produce has to look like one. It has nothing to say
            # about the graph: the vault is still there, and a picture of it
            # that freezes the moment you mute the microphone would be saying
            # something about the microphone that is not true.
            return True
        return self.ambient() != "muted" or self._intensity() < 1.0

    def _tick(self) -> float:
        """How long to wait for a key before repainting for the animation.

        Per state, and not out of thrift for its own sake — but the thrift is
        now cheap. What made the old clock slow was that a frame meant the
        *whole screen*; with `_repaint_stage()` a frame is a thirty-column
        rectangle, so the states that move fast can have the frame rate they
        were always drawn for. Idle still runs slower than the rest: it is
        the state this window spends its life in, and its motion is a drift
        that gains nothing from being sampled twice as often.

        The graph overrides all of that, and it is allowed to because it is
        the one thing here whose motion is continuous rather than a loop: the
        aperture's particles are a breathing pattern that reads the same at
        eighteen frames as at forty-five, while a field of nodes panning
        across a character grid does not — under about thirty it stops
        gliding and starts *stepping*, one cell at a time, which looks like a
        fault rather than a drift. So it gets forty-five, and the canvas,
        being four times the rectangle, gets thirty.
        """
        if not self._animating():
            return 0.25
        rate = {"thinking": 1 / 30, "speaking": 1 / 30, "wake": 1 / 36,
                "listening": 1 / 30}.get(self.ambient(), 1 / 18)
        if self._graph and (self.canvas or self._graph_h):
            # A period, so the faster clock is the smaller number. The canvas
            # keeps up with the side pane rather than running slower for being
            # four times the rectangle, because the aperture is no longer
            # redrawn on every one of its frames — see `_repaint_stage`, which
            # is what bought the difference.
            return min(rate, 1 / 45)
        return rate

    def _window_command(self, line: str) -> bool:
        """The two commands that are about this window rather than about V.

        They live here rather than in the CLI's table because there is nothing
        for the scrolling prompt to do with either of them: it has no aperture
        to mute and no pane to walk through the states.
        """
        cmd = line.split(" ")[0]
        if cmd == "/mute":
            self.muted = not self.muted
            # 0ms into muted, 800ms out of it — the asymmetry is deliberate.
            # Going dark has to be instantaneous or it cannot be trusted.
            self._unmuted_at = 0.0 if self.muted else time.time()
            self._demo = None
            self.say(Entry("plain",
                           "_Muted. Nothing is listening until you say "
                           "`/mute` again._" if self.muted else
                           "_Listening again._"))
            return True
        if cmd == "/mouse":
            self.mouse = not self.mouse
            self._clear_selection()
            with self._paint:
                self._out.write(MOUSE_ON if self.mouse else MOUSE_OFF)
                self._out.flush()
            self.say(Entry("plain",
                           "_The mouse is mine again — the wheel scrolls the "
                           "transcript, and a drag over it copies the text._"
                           if self.mouse else
                           "_The mouse is the terminal's. Selecting works the "
                           "way it does everywhere else — borders and all — "
                           "and the wheel does nothing; PgUp/PgDn scroll._"))
            return True
        if cmd == "/ambient":
            self._demo = list(ui.AMBIENT_ORDER)
            self._demo_at = time.time()
            self.say(Entry("plain",
                           "_Walking the aperture through every state it "
                           "has — about sixteen seconds. Type to stop it._"))
            return True
        if cmd == "/graph":
            self.canvas = not self.canvas
            self._view_key = ()           # whole vault and neighbourhood are
            self._clear_selection()       # not the same picture
            self.say(Entry("plain",
                           "_The whole vault, where the transcript was. It "
                           "keeps drawing while V works — the route is under "
                           "the picture. `/graph` again to read._"
                           if self.canvas else
                           "_Back to the transcript._"))
            return True
        return False

    def run(self) -> int:
        self._enter()
        # Kicked off before the first frame and waited on by nothing. The
        # window is fully drawable without it; the graph simply appears in the
        # pane a moment later, the first time V reaches for a note.
        self._ensure_graph()
        # A resize has to redraw immediately: the frame is absolute-positioned,
        # so a window that changes size and is not redrawn leaves the old
        # border painted across the new one.
        try:
            # Set a flag; do not draw. A signal handler runs on whichever
            # thread happens to be executing, so drawing from here re-enters
            # the paint lock the interrupted thread is already holding — which
            # is a deadlock on a non-reentrant lock and, when it does get
            # through, paints one frame inside another. The loop below picks
            # the flag up on its next idle tick.
            signal.signal(signal.SIGWINCH,
                          lambda *_: setattr(self, "_resized", True))
        except (ValueError, AttributeError):
            pass
        interrupted = False
        try:
            self.draw()
            while True:
                # The tick is the aperture's frame rate and nothing else's. It
                # only runs while something in the pane is actually moving —
                # muted and reduced motion both settle to a still frame, and a
                # still frame that keeps being repainted is just heat.
                key = self._read(self._tick())
                if key is None:
                    if self._resized:
                        self._resized = False
                        self._clear_selection()
                        self.draw()
                        continue
                    if self._flash and time.time() >= self._flash_until:
                        self._flash = ""
                        self.draw()
                        continue
                    self._tick_demo()
                    if self._animating() and not self._repaint_stage():
                        self.draw()
                    continue
                if key.startswith("\x1b[<") and self._mouse(key):
                    # Drawn straight away rather than left to the frame clock:
                    # a drag has to keep up with the pointer or the selection
                    # reads as lagging behind the hand making it.
                    self.draw()
                    continue
                if self._sel:
                    # Any key at all ends the selection, which is what a
                    # terminal does — you have stopped pointing at the
                    # transcript and gone back to writing in it.
                    self._clear_selection()
                if key == "\x03":                           # ctrl-c
                    if interrupted or self.buf:
                        if self.buf:
                            self.buf, self.cur, interrupted = "", 0, True
                            self.draw()
                            continue
                        return 0
                    interrupted = True
                    self.draw()
                    continue
                if key == "\x04" and not self.buf:           # ctrl-d
                    return 0
                if key == "\x0c":                            # ctrl-l
                    self._out.write("\x1b[2J")
                    self.draw()
                    continue
                interrupted = False
                line = self._edit(key)
                if line is None:
                    self.draw()
                    continue
                line = line.strip()
                if not line:
                    self.draw()
                    continue
                self.history.append(line)
                if line in ("/quit", "/exit", "quit", "exit"):
                    return 0
                if self._window_command(line):
                    self.draw()
                    continue
                self.say(Entry("user", line))
                self._turn(line)
                self.draw()
        finally:
            self._exit()
            if self.on_exit:
                self.on_exit(self.history)
        return 0

    def progress(self, ev) -> None:
        """What the run in flight is doing, from `shells.ShellEvent`.

        Called from the shell's reader thread while `_turn` below is blocked
        on the run, so it does nothing but assign — no drawing, no locks. The
        frame is already being repainted 30 times a second by `tick()`, and
        picking these up on the next frame is exactly right; taking a lock to
        deliver them sooner would put the reader thread and the renderer in
        each other's way for a difference nobody can see.

        Assigning `self.tools` in one thread and reading it in another is safe
        for the same reason it is enough: an int assignment is atomic in
        CPython, and a frame drawn with last frame's count is a frame nobody
        notices.
        """
        kind = getattr(ev, "kind", "")
        if kind == "tool":
            self._inflight += 1
            self.tools = max(1, min(self._inflight, 5))
            self._step = ev.tool.split("__")[-1] or "working"
            # The note this call is about, named in its argument — so the
            # graph moves when the call goes *out*, not when it comes back.
            self._touch(getattr(ev, "rel", ""))
        elif kind == "tool_result":
            self._inflight = max(0, self._inflight - 1)
            self.tools = max(1, self._inflight)
            # And the notes it came back holding. A search names eight at
            # once, and so does a read of a note full of wikilinks — both are
            # honestly "what that call just put in front of V", which is what
            # the constellation is showing. They do not take the focus: the
            # note being read stays the one under the mark.
            for rel in getattr(ev, "rels", ()) or ():
                self._touch(rel, focus=False)
        elif kind == "text" and self._step != "writing":
            self._step = "writing"
            self._inflight = 0
            self.tools = 1
            # Nothing is being read any more. The route stays lit — it is what
            # the answer was built out of — but there is no longer a *here*.
            self._active = ""

    def _turn(self, line: str) -> None:
        """Run one message with the frame animating around it.

        The engine call stays on this thread. Its SQLite connection belongs to
        whichever thread opened it, so moving the work to a worker and drawing
        from here — which is the obvious arrangement — dies on the first query
        with "SQLite objects created in a thread can only be used in that same
        thread". Instead the work blocks and a watchdog thread ticks the
        spinner, which is the only thing that needs to move.
        """
        import threading

        self.busy = True
        # A new question is a new route. The last turn's constellation is
        # cleared here rather than when that turn ended, so the picture of what
        # the answer was built from stays up for as long as the answer does.
        self._trail, self._active, self._view_key = [], "", ()
        self._ensure_graph()
        stop = threading.Event()
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        t0 = time.time()
        verb = ui.VERBS[int(t0) % len(ui.VERBS)]

        def tick() -> None:
            # Two clocks, because the two things move at different speeds: the
            # braille spinner is a ten-frame loop that reads as fast at 12fps
            # and as a blur above it, while the aperture wants every frame it
            # can get. The spinner line lives in the transcript, so changing
            # it costs a whole frame; the aperture in between costs a corner.
            i, drawn = 0, 0.0
            while not stop.wait(1 / 30):
                el = time.time() - t0
                if el - drawn >= 0.08:
                    drawn = el
                    secs = f" {ui.DIM3}{el:.0f}s{ui.RESET}" if el >= 2 else ""
                    # The generic verb until the run says otherwise, then the
                    # name of what it is actually doing. A minute of
                    # "thinking…" and a minute of "vault_search… gmail_read…
                    # writing…" are the same wait; only one of them is legible.
                    label = self._step or verb
                    self.status = (f"{ui.INDENT}{ui.SEED}{frames[i % 10]}"
                                   f"{ui.RESET} {ui.DIM2}{label}…{ui.RESET}"
                                   f"{secs}")
                    i += 1
                    self.draw()
                elif self._animating() and not self._repaint_stage():
                    self.draw()

        spinner = threading.Thread(target=tick, daemon=True)
        spinner.start()
        try:
            if line.startswith("/") and self.on_command:
                entries = self.on_command(line)
            else:
                entries = self.handle(line)
        except Exception as exc:                # a bad turn must not kill it
            entries = [Entry("plain",
                             f"_{type(exc).__name__}: {exc}_")]
        finally:
            stop.set()
            spinner.join(timeout=0.3)
            self.status = ""
            self.busy = False
            # Back to one ring. A count left over from the last turn would
            # have the aperture claiming work that finished minutes ago — and
            # the same goes for the mark on the graph. The route itself stays:
            # it is the shape of the answer that is about to be printed.
            self._step, self._inflight, self.tools = "", 0, 1
            self._active = ""
        for e in entries or []:
            self.say(e)
        # The answer is now on screen, and on the ambient device this is the
        # stretch where V is actually saying it out loud. The pane shows that
        # state for about as long as the answer takes to read — one emit ring
        # per sentence, and no second loop on the same one.
        spoken = sum(len(e.text) for e in entries or [] if e.kind == "answer")
        self._speak_until = time.time() + min(4.0, 0.7 + spoken / 260.0)


def usable() -> bool:
    """Whether a real window can be drawn at all.

    False under a pipe, under `--plain`, in a dumb terminal, or on a platform
    with no `termios` — every one of which still has to work, so the caller
    falls back to the scrolling prompt rather than failing.
    """
    if not ui.COLOUR or os.environ.get("V_NO_TUI"):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()
