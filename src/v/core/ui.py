"""ui.py — the terminal face of V. Presentation only; no vault, no engine.

Everything here is stdlib. The rest of this codebase earns its keep by being
free and local, and a terminal that needs a rendering framework to draw a
bullet would be the one place that broke the rule.

Three jobs:

* **Colour and markdown.** `Response.text` is markdown — `**bold**`, `· `
  bullets, `[[wikilinks]]`, fenced code. The old REPL printed the asterisks.
* **Motion.** A spinner while the engine thinks, because a REPL that goes
  silent for four seconds during a `hermes` call looks hung, not busy.
* **Line editing.** `input()` only gets arrow keys, history and bracketed
  paste once `readline` is imported *and* the prompt's escape codes are
  wrapped in \\001/\\002 — without those markers readline miscounts the
  prompt width and left-arrow walks over the `›`. That was the whole bug.

Nothing *here* captures the mouse or switches to the alternate screen, which
is what keeps the terminal's own select-to-copy working on the scrolling
prompt's output. The window in `tui.py` does both, and owns the consequences.
"""

from __future__ import annotations

import atexit
import base64
import itertools
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time

# --------------------------------------------------------------------------
# colour


def _supports_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "") in ("dumb", ""):
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


COLOUR = _supports_colour()


def _s(seq: str) -> str:
    return seq if COLOUR else ""


RESET = _s("\x1b[0m")
BOLD = _s("\x1b[1m")
DIM = _s("\x1b[2m")
ITALIC = _s("\x1b[3m")


def _c(n: int) -> str:
    return _s(f"\x1b[38;5;{n}m")


# The design tokens, as 256-colour indices. The design specifies hex for the
# screen surfaces and an ANSI fallback for this one, and the fallback is what
# the terminal actually gets — truecolour would look marginally closer and
# would stop working over half the SSH sessions this runs in.
#
# Two rules from the spec that these encode:
#   · `refuse` is for refusals, never for errors. A refusal is V holding a
#     rule, not V failing, and colouring the two the same teaches you to read
#     a held rule as a bug.
#   · The register is level, so nothing here is louder than anything else.
#     Severity is carried by which glyph is chosen and where the line sits.
SEED = _c(99)        # #7C5CFF — V's voice, the ✦, the prompt, focus
DEEP = _c(62)        # #5B3FD6 — structure: gutters, rules, the quiet half
LIFT = _c(141)       # #A793FF — highlight violet, wikilinks, emphasis
CORE_HI = _c(189)    # #D8CEFF — the specular highlight, used sparingly

DIM3 = _c(239)       # least-important mono
DIM2 = _c(240)       # the cost line, metadata
DIM1 = _c(241)       # section labels
MUTED = _c(243)      # secondary body copy
BODY = _c(244)       # body copy
BODY_HI = _c(246)    # emphasised body copy, inline code
PAPER = _c(253)      # what V says
PAPER_HI = _c(255)   # primary text, and your own input echoed back

UNSURE = _c(179)     # #E8A33D — low confidence, held, budget warning
REFUSE = _c(203)     # #FF6B6B — refusal. Never "error".
FREE = _c(79)        # #5CD9A6 — local, zero-cost, committed, online
GREY = _c(238)       # #3B3B49 — degraded / local-only voice

LINE = _c(235)       # card border
LINE_2 = _c(236)     # inner divider
LINE_3 = _c(237)     # emphasised border

# The old names, kept pointing at their nearest token so every existing call
# site keeps working. New code should use the tokens above.
ACCENT = SEED
ACCENT_D = DEEP
CYAN = LIFT
GREEN = FREE
YELLOW = UNSURE
RED = REFUSE
CODE = BODY_HI
LINK = LIFT

# ── glyphs ────────────────────────────────────────────────────────────────
# Single mono characters. No icon font, no SVG, nothing to load — which is
# also what lets the same six marks work in a terminal, in a notification and
# read aloud.
VOICE = "✦"          # everything V says in text. Never a logo, never decorative.
G_ROUTE = "◈"        # route / model
G_NOTE = "⟡"         # memory / vault note
G_CONF = "◉"         # confidence
G_COST = "$"         # cost
G_LOCAL = "●"        # local, nothing left the machine
G_REFUSED = "▲"      # refused

# The four states V's voice can be in, and nothing else may use these colours
# for text V speaks.
_VOICE_COLOUR = {
    "speaking": SEED,
    "unsure": UNSURE,
    "refused": REFUSE,
    "degraded": GREY,
}

INDENT = "  "
# ✦ + one space. Continuation lines and the cost line hang to this, so an
# answer reads as one block rather than as a glyph with debris under it.
HANG = INDENT + "  "
# Public: `tui.py` walks the same escapes to light a selection without
# charging them width.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Every name above that holds an escape sequence, so `--plain` can blank them
# in one pass instead of threading a flag through every call site.
_STYLE_NAMES = (
    "RESET", "BOLD", "DIM", "ITALIC",
    "SEED", "DEEP", "LIFT", "CORE_HI",
    "DIM3", "DIM2", "DIM1", "MUTED", "BODY", "BODY_HI", "PAPER", "PAPER_HI",
    "UNSURE", "REFUSE", "FREE", "GREY", "LINE", "LINE_2", "LINE_3",
    "ACCENT", "ACCENT_D", "CYAN", "GREEN", "YELLOW", "RED", "CODE", "LINK",
)


def disable_colour() -> None:
    """`--plain`. Also kills the spinner and the settle, which need a TTY."""
    global COLOUR
    COLOUR = False
    for name in _STYLE_NAMES:
        globals()[name] = ""
    # The voice map holds copies of the escapes, so blanking the names above
    # would leave `answer()` still emitting colour into a plain pipe.
    for key in _VOICE_COLOUR:
        _VOICE_COLOUR[key] = ""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# Everything below U+0300 is one column wide, uncombining, and unaffected by
# NFC normalisation — which is to say: all of ASCII, and the Latin-1 letters.
# `_SIMPLE` is that set plus the two blocks this window draws its own pictures
# out of, box-drawing and braille, neither of which is wide or combining
# either. A line made only of these can be measured by counting it.
_SIMPLE = re.compile(r"^[\x20-˿─-╿⠀-⣿]*$")


def cell_width(text: str) -> int:
    """How many columns `text` actually occupies, which is not `len(text)`.

    The vault is half Persian, and `len()` is wrong for it in both directions:
    ZWNJ (the نیم‌فاصله this vault is full of) and combining marks are counted
    as characters but drawn as nothing, while CJK and most emoji are one
    character and two columns. Padding a table with `len()` is why every
    column with Persian in it came out ragged.

    The fast path is not premature. This is the most-called function in the
    module by an order of magnitude — every `pad`, every `clip`, every cell of
    every frame goes through it — and at forty-five frames a second the
    per-character `east_asian_width` lookup was thirty per cent of the time
    spent drawing. The slow path below is still the only correct answer for
    Persian, and Persian still takes it.
    """
    plain = strip_ansi(text)
    if _SIMPLE.match(plain):
        return len(plain)

    import unicodedata

    w = 0
    for ch in unicodedata.normalize("NFC", plain):
        # Bidi controls (isolates and embeddings) join the zero-width set:
        # they are characters that draw nothing, and counting them is how a
        # Persian column comes out one cell short per run.
        if unicodedata.combining(ch) or ch in "​‌‍‎‏﻿⁦⁧⁨⁩‪‫‬‭‮":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(text: str, n: int) -> str:
    return text + " " * max(0, n - cell_width(text))


def truncate(text: str, n: int) -> str:
    """Cut to `n` columns, with an ellipsis when something was lost."""
    if cell_width(text) <= n:
        return text
    if n <= 1:
        return "…"[:n]
    out, w = [], 0
    for ch in text:
        cw = cell_width(ch)
        if w + cw > n - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


# Set by a surface that owns a region smaller than the terminal — the window's
# transcript pane. Without it every handler that calls `width()` measures the
# whole terminal and writes lines that walk straight through the border.
WIDTH_OVERRIDE: int | None = None


def clip(styled: str, n: int) -> str:
    """Cut a *styled* line to `n` columns without cutting an escape in half.

    `truncate()` measures characters and would count the bytes of a colour
    sequence as width, so a coloured line comes out mangled and short. This
    walks the escapes, charges them nothing, and closes with a reset so the
    colour cannot leak into whatever is drawn to the right of it.
    """
    if cell_width(styled) <= n:
        return styled
    out, w, i = [], 0, 0
    while i < len(styled):
        m = ANSI_RE.match(styled, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        cw = cell_width(styled[i])
        if w + cw > n - 1:
            break
        out.append(styled[i])
        w += cw
        i += 1
    return "".join(out) + "…" + RESET


def width(default: int = 80, cap: int = 96) -> int:
    """Usable text width. Capped: prose set to a 200-column terminal is unread."""
    if WIDTH_OVERRIDE:
        return WIDTH_OVERRIDE
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        cols = default
    return max(40, min(cols - len(INDENT) - 1, cap))


# --------------------------------------------------------------------------
# markdown → ANSI
#
# Deliberately partial. It covers what `Response.text` actually contains —
# headings, bold, bullets, code, wikilinks, tables — and leaves anything it
# doesn't recognise alone rather than guessing.

_BULLET_RE = re.compile(r"^(\s*)([-*+·•]|\d+\.)\s+(.*)$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")

# Arabic, Arabic Supplement, Extended-A, and the Presentation Forms. Persian
# is written in this range; the vault is ~17% of it.
_RTL_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ"
                     r"ﭐ-﷿ﹰ-﻿]")
# Bidi isolates. Without them a Persian sentence sitting inside LTR chrome
# drags the punctuation and any Latin words in it to the wrong end — the line
# is stored correctly and *displays* scrambled, which is the worst kind of
# wrong because the note looks broken and isn't.
_RLI, _PDI = "⁧", "⁩"


_RTL_RUN = re.compile(r"[^\x00-\x7f]*[؀-ۿݐ-ݿࢠ-ࣿ"
                      r"ﭐ-﷿ﹰ-﻿][^\x00-\x7f]*"
                      r"(?:[ ‌][^\x00-\x7f]+)*")


def isolate(text: str) -> str:
    """Wrap each Persian run in a bidi isolate, leaving the English alone.

    Per *run*, not per line, and this is the whole lesson of getting it wrong
    once. Isolating the line sets its paragraph direction to RTL, which is
    correct by the standard and awful in a task list: `مرور کنم (overdue, due
    2026-07-26)` is stored with the date last and renders with it thrown to the
    far left, so every carried task looked like it began with its own metadata.

    Isolating each run instead keeps the line's direction LTR — the terminal's
    direction, and the direction of the gutter, the bullet and the timestamps
    around it — while still letting the Persian inside each run shape and
    order correctly.
    """
    return _RTL_RUN.sub(lambda m: _RLI + m.group(0) + _PDI, text)


def is_rtl(line: str) -> bool:
    """True when a line is predominantly Persian.

    Counted against letters only. A line like `- [ ] بازبینی MR-758` is Persian
    with Latin in it, and thresholding on the whole string would call it
    English because of the digits and the markup.
    """
    letters = [ch for ch in strip_ansi(line) if ch.isalpha()]
    if not letters:
        return False
    return sum(bool(_RTL_RE.match(ch)) for ch in letters) / len(letters) >= 0.4


def _inline(text: str, base: str = "") -> str:
    """Inline spans. `base` is the style to return to when a span closes."""
    if not COLOUR:
        return _plain(text)

    def wrap(style):
        return lambda m: f"{style}{m.group(1)}{RESET}{base}"

    text = re.sub(r"`([^`\n]+)`", wrap(CODE), text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", wrap(BOLD + base), text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", wrap(ITALIC), text)
    # `_like this_` too. The engine writes its asides in that form — every
    # "no model is configured" note, every degraded excuse — and they were
    # reaching the screen with the underscores still on.
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", wrap(ITALIC), text)
    text = re.sub(r"\[\[([^\]\n]+)\]\]", wrap(LINK), text)
    return isolate(text)


def _plain(text: str) -> str:
    """The same string with markers removed — used to measure, never printed."""
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", text)
    text = re.sub(r"\[\[([^\]\n]+)\]\]", r"\1", text)
    return text


def _wrap(plain: str, styled: str, cols: int, first: str, rest: str) -> list[str]:
    """Wrap `plain` to `cols`, then re-attach the styling word for word.

    Styling never adds or removes a space, so the styled string has exactly the
    words the plain one does; wrapping the plain text tells us how many words
    go on each line and the styled words follow. A bold run that straddles a
    break keeps working because the terminal carries the attribute across the
    newline — there is no reset in between to undo it.
    """
    # `first` and `rest` carry colour codes; measuring them with len() would
    # charge the line a dozen columns for bytes the terminal never draws, and
    # every bullet would wrap early for no visible reason.
    body = cols - cell_width(first)
    if body <= 8:
        return [first + styled]
    lines = textwrap.wrap(plain, width=body, break_long_words=False,
                          break_on_hyphens=False) or [""]
    words = styled.split()
    out, i = [], 0
    for n, line in enumerate(lines):
        take = len(line.split())
        chunk = " ".join(words[i:i + take]) if take else ""
        i += take
        out.append((first if n == 0 else rest) + chunk)
    if i < len(words):  # measurement disagreed; don't lose text over it
        out[-1] += " " + " ".join(words[i:])
    return out


def markdown(text: str, cols: int | None = None, indent: str = INDENT,
             rtl_colour: str | None = None) -> str:
    """Render markdown to ANSI, wrapped and indented for a terminal.

    `rtl_colour` tints the gutter beside Persian blocks — the caller passes the
    confidence colour so a shaky transcript is visible at the block, which is
    the one place you can still fix it.
    """
    cols = cols or width()
    rtl_colour = rtl_colour or LINE_3
    out: list[str] = []
    fence: str | None = None
    # Set while we're inside a list item, so a line that continues one gets
    # the item's hanging indent instead of falling back to the left margin.
    # Markdown agrees: without a blank line between them, that line *is* part
    # of the item above it.
    hang: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        hang, prev_hang = None, hang

        m = _FENCE_RE.match(line)
        if m:
            fence = None if fence is not None else (m.group(1) or "")
            continue
        if fence is not None:
            out.append(f"{indent}{LINE_2}│{RESET} {CODE}{line}{RESET}")
            continue

        if not line.strip():
            out.append("")
            continue

        # Tables are already laid out to fit — `Result.table()` measures and
        # truncates against this same width. Wrapping one would undo that work
        # and destroy the alignment with it.
        if line.lstrip().startswith("|") or "│" in line or "─┼─" in line:
            # The rule between head and body is chrome; the rows are the data
            # and dimming those would be dimming the answer.
            faint = LINE_2 if set(line.strip()) <= set("─┼─| -") else BODY
            out.append(f"{indent}{faint}{_plain(line)}{RESET}")
            continue
        if set(line.strip()) <= set("-—─=_ ") and len(line.strip()) > 2:
            out.append(f"{indent}{LINE_2}{'─' * min(len(line.strip()), cols)}{RESET}")
            continue

        m = _HEAD_RE.match(line)
        if m:
            level, body = len(m.group(1)), m.group(2)
            style = SEED + BOLD if level <= 2 else BOLD + PAPER_HI
            out.append(f"{indent}{style}{_plain(body)}{RESET}")
            continue

        # A line that is nothing but bold is a heading in every practical
        # sense — `**V — status**`, `**Today**` — and reads like one.
        if re.fullmatch(r"\*\*[^*]+\*\*:?", line.strip()):
            body = line.strip().rstrip(":").strip("*")
            out.append(f"{indent}{SEED}{BOLD}{body}{RESET}")
            continue

        # `↳ path/to/note` — the engine's "and this is where it lives" line.
        # It belongs to the bullet above it, so it gets that bullet's indent
        # and steps back out of the way.
        if line.lstrip().startswith("↳"):
            body = line.lstrip()[1:].strip()
            hang = prev_hang or f"{indent}  "
            out.append(f"{hang}{DIM3}↳ {body}{RESET}")
            continue

        # A Persian line gets its own block: gutter, isolate, and no attempt to
        # mix it into an English sentence. The gutter stays on the left even
        # though the text runs right-to-left — it is chrome, and the terminal's
        # chrome is LTR. Flipping it would mean padding every line to a fixed
        # column, which breaks the moment the window is resized.
        m = _BULLET_RE.match(line)
        if m:
            lead, _, body = m.groups()
            first = f"{indent}{lead}{DEEP}•{RESET} "
            hang = f"{indent}{lead}  "
            out += _wrap(_plain(body), _inline(body), cols + len(indent),
                         first, hang)
            continue

        if line.lstrip().startswith(">"):
            body = line.lstrip()[1:].strip()
            first = f"{indent}{LINE_2}│{RESET} {MUTED}"
            out += [ln + RESET for ln in
                    _wrap(_plain(body), _inline(body, base=MUTED),
                          cols + len(indent), first,
                          f"{indent}{LINE_2}│{RESET} {MUTED}")]
            continue

        # A plain line straight after a list item, with no blank line between
        # them, belongs to that item — the nudge digest and the task views
        # both emit their detail and citations that way. Give it the item's
        # hanging indent so it reads as attached rather than as a new
        # paragraph that happens to sit there.
        lead = prev_hang or indent
        out += _wrap(_plain(line), _inline(line), cols + len(indent),
                     lead, lead)
        hang = prev_hang

    return "\n".join(out)


# --------------------------------------------------------------------------
# V's voice
#
# `✦` prefixes everything V says in text and nothing else in this program may
# use it. Its colour is the whole state vocabulary — speaking, unsure, refused,
# degraded — which is why a refusal needs no red banner and a degraded answer
# needs no warning box: the glyph already said it, in the same level tone as
# every other answer.


def conf_colour(confidence: float | None) -> str:
    """The colour a confidence score earns. ≥0.8 reads as settled."""
    if confidence is None:
        return LINE_3
    return FREE if confidence >= 0.8 else UNSURE


def voice(state: str = "speaking") -> str:
    return f"{_VOICE_COLOUR.get(state, SEED)}{VOICE}{RESET}"


def answer(text: str, *, state: str = "speaking",
           confidence: float | None = None, cols: int | None = None) -> str:
    """One thing V said: the ✦, then the answer hung under it.

    The glyph is spliced into the first line that has content rather than
    printed on its own — a `✦` alone on a line reads as a bullet for the
    paragraph below it instead of as V starting to speak.
    """
    body = markdown(text, cols=cols, indent=HANG,
                    rtl_colour=conf_colour(confidence)
                    if confidence is not None else None)
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = f"{INDENT}{voice(state)} " + line[len(HANG):]
            break
    return "\n".join(lines)


def costline(*, route: str = "", model: str = "", notes: int | None = None,
             seconds: float | None = None, cost: str = "",
             confidence: float | None = None,
             extra: "list[str] | None" = None) -> str:
    """`◈ sonnet · ⟡ 3 notes · 1.24s · ≈$0.0031 allowance · ◉ 0.94`

    Under every answer, in every surface, never behind a flag. It is the one
    line that says what V just spent on your behalf and how sure it is, and
    the moment it becomes optional it becomes something you stop reading.
    """
    def part(glyph: str, value: str, tint: str = BODY_HI) -> str:
        lead = f"{DIM2}{glyph} " if glyph else ""
        return f"{lead}{tint}{value}{DIM2}"

    bits: list[str] = []
    if model or route:
        bits.append(part(G_ROUTE, model or route))
    if notes:
        bits.append(part(G_NOTE, f"{notes} note{'s' if notes != 1 else ''}"))
    if seconds is not None:
        bits.append(part("", f"{seconds:.2f}s" if seconds < 100 else f"{seconds:.0f}s"))
    if cost:
        bits.append(part("", cost, FREE if cost.startswith("$0.0000") else BODY_HI))
    if confidence is not None:
        bits.append(part(G_CONF, f"{confidence:.2f}", conf_colour(confidence)))
    bits += [part("", e, BODY) for e in (extra or [])]
    if not bits:
        return ""
    return HANG + " · ".join(bits) + RESET


def card(header: str, body: str, colour: str = SEED,
         cols: int | None = None) -> str:
    """A named state — LOW CONFIDENCE, REFUSED, DEGRADED — and what it means.

    The header is the only thing that carries the state's colour. The body
    stays the same weight as any other answer, because the register does not
    move: a refusal is delivered at the volume of a merged MR.
    """
    # The spec letter-spaces this header. At `LOW CONFIDENCE · THE TRANSCRIPT
    # IS WRONG` a per-character gap runs past eighty columns and stops being
    # readable, so the terminal takes the uppercase and the colour and leaves
    # the tracking to the surfaces that have proportional type.
    out = [f"{INDENT}{colour}{header.upper()}{RESET}"]
    out.append(markdown(body, cols=cols, indent=HANG))
    return "\n".join(out)


def bar(fraction: float, cols: int = 22, warn: float = 0.8) -> str:
    """A spent/remaining track. Turns amber late rather than red at the end."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * cols)
    tint = UNSURE if fraction >= warn else DEEP
    return (f"{tint}{'━' * filled}{RESET}{LINE_2}{'━' * (cols - filled)}{RESET}")


def pips(used: int, cap: int) -> str:
    """The proactive budget, spent by interruption rather than by tokens."""
    return (f"{SEED}{'▮' * used}{RESET}{LINE_2}{'▮' * max(0, cap - used)}{RESET}")


def proactive(text: str, *, cols: int | None = None,
              footer: str = "unprompted · dismiss with /quiet") -> str:
    """Something V said that you did not ask for.

    Marked for what it is. An unprompted line that looks identical to an answer
    is how these things get muted: you go looking for the question you asked
    and find one you didn't.
    """
    body = markdown(text, cols=cols, indent="")
    out = [f"{INDENT}{DEEP}┃{RESET} {voice()} {PAPER}{line}{RESET}"
           if i == 0 else f"{INDENT}{DEEP}┃{RESET}   {PAPER}{line}{RESET}"
           for i, line in enumerate(body.split("\n"))]
    out.append(f"{INDENT}{DIM3}{footer}{RESET}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# motion

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_OFF = False
# Said while it works. Not decoration: each one names a thing V actually does,
# so the word on screen is a rough answer to "what is it doing".
VERBS = ("thinking", "reading the vault", "recalling", "checking",
         "putting it together", "looking it up")


def _show_cursor() -> None:
    # A crash mid-spin would otherwise leave the shell with no cursor, and
    # nothing about `reset` is obvious to someone who just wanted a brief.
    if COLOUR:
        try:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()
        except Exception:
            pass


atexit.register(_show_cursor)


class Spinner:
    """A braille spinner on one line, erased without a trace when it stops.

    Writes nothing at all when stdout is not a terminal, so piping the CLI
    into a file still produces clean output.
    """

    def __init__(self, label: str = "thinking", stream=None, delay: float = 0.25):
        self.label = label
        self.stream = stream or sys.stdout
        self.delay = delay          # grace period — don't flash on a 12ms answer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = False
        self.t0 = 0.0
        # Steps waiting to be printed above the spinner. A wait longer than a
        # few seconds with nothing on screen reads as hung; the spec's answer
        # is that the route appears as it happens, so the wait is legible
        # rather than blank. One lock because `step()` is called from the main
        # thread and drained by the animation thread.
        self._steps: list[str] = []
        self._lock = threading.Lock()

    def step(self, text: str) -> None:
        """Name something V just did. Printed above the spinner, in order."""
        with self._lock:
            self._steps.append(text)

    def _drain(self) -> None:
        with self._lock:
            steps, self._steps = self._steps, []
        for text in steps:
            self.stream.write(f"\r\x1b[2K{HANG}{DIM3}{G_ROUTE} {text}{RESET}\n")

    def _run(self) -> None:
        frames = itertools.cycle(_SPIN)
        while not self._stop.wait(0.08):
            elapsed = time.time() - self.t0
            if elapsed < self.delay:
                continue
            if not self._drawn:
                self.stream.write("\x1b[?25l")
                self._drawn = True
            self._drain()
            secs = f" {DIM2}{elapsed:.0f}s{RESET}" if elapsed >= 2 else ""
            self.stream.write(
                f"\r\x1b[2K{INDENT}{SEED}{next(frames)}{RESET} "
                f"{DIM2}{self.label}…{RESET}{secs}")
            self.stream.flush()

    def start(self) -> "Spinner":
        # SPINNER_OFF is set while a surface that draws its own progress runs
        # a handler that would otherwise start a second one. Two spinners in
        # one turn is not just noise: the inner one writes carriage returns
        # and erase-line escapes into output the outer one is capturing.
        if not COLOUR or SPINNER_OFF:
            return self
        self.t0 = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self._drawn:
            self.stream.write("\r\x1b[2K\x1b[?25h")
            self.stream.flush()
            self._drawn = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def settle(text: str, stream=None, stagger: float = 0.12,
           budget: float = 0.4) -> None:
    """The answer arrives, line by line, rather than appearing all at once.

    Deliberately not a typewriter: characters do not crawl, whole lines land.
    The spec's stagger is 120ms, which is right for the three-line answers
    that make up most of what V says and absurd for a fifty-row table — so the
    whole arrival is capped at `budget` and long output effectively snaps in.

    Off when piped or when V_NO_ANIM is set. Nothing about the layout changes
    when it is off, which is the test: if motion is the only thing telling you
    something happened, the state was never designed.
    """
    stream = stream or sys.stdout
    lines = text.split("\n")
    if not COLOUR or os.environ.get("V_NO_ANIM") or not lines:
        stream.write(text + "\n")
        stream.flush()
        return
    pause = min(stagger, budget / max(len(lines), 1))
    for line in lines:
        stream.write(line + "\n")
        stream.flush()
        if pause > 0.0005:
            time.sleep(pause)


def beat(seconds: float = 0.2) -> None:
    """The pause before the cost line. It belongs to the answer, not to it."""
    if COLOUR and not os.environ.get("V_NO_ANIM"):
        time.sleep(seconds)


# The old name. `reveal` said what it did to the screen; `settle` says what it
# does to the answer, which is the thing the spec cares about.
reveal = settle


# --------------------------------------------------------------------------
# the ambient vessel
#
# The side pane has its own terminal-native identity: an open V whose edges
# settle into a short basin. It is not a rasterised logo. Each state keeps that
# silhouette and changes the energy it catches, converges, or emits. The ✦
# remains reserved for V's textual voice in the transcript.
#
# These are the content bindings used by the surrounding stage. Geometry and
# motion are defined together in aperture(), so a new state cannot accidentally
# acquire a label without also deciding what it looks like.
AMBIENT: "dict[str, dict]" = {
    "idle": {
        "label": "IDLE", "meta": "nothing leaves the room",
        "caption": "Hearing. Not recording.",
        "wave": "flat",
    },
    "wake": {
        "label": "WAKE HEARD", "meta": "“V”",
        "caption": "Yes?",
        "wave": "flat",
    },
    "listening": {
        "label": "LISTENING", "meta": "buffering",
        "caption": "Not sent yet.",
        "wave": "live",
    },
    "thinking": {
        "label": "THINKING", "meta": "working",
        "caption": "…",
        "wave": "flat",
    },
    "speaking": {
        "label": "SPEAKING", "meta": "answering",
        "caption": "",
        "wave": "speak",
    },
    "muted": {
        "label": "HARD MUTED", "meta": "mic off",
        "caption": "Nothing here is listening.",
        "wave": "off",
    },
}

# The order `/ambient` walks them in — the order they actually happen in.
AMBIENT_ORDER = ("idle", "wake", "listening", "thinking", "speaking", "muted")


def _grid_lines(g: list) -> list:
    """Grid → styled lines, one escape per colour run rather than per cell."""
    out = []
    for row in g:
        parts, last = [], None
        for ch, tint in row:
            if tint != last:
                parts.append(tint or RESET)
                last = tint
            parts.append(ch)
        out.append("".join(parts) + RESET)
    return out


_PARTICLES = ".·:•●"


def _particle_noise(x: float, y: float, seed: int = 0) -> float:
    """Stable, dependency-free lattice noise in the interval 0..1."""
    value = math.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453
    return value - math.floor(value)


def _flow(x: float, y: float, seed: int = 0) -> float:
    """The same noise, smoothly interpolated and sampled at a *fractional*
    point — a field that can be slid under the character grid.

    This is the whole difference between dust that blinks and dust that
    travels. Rolling fresh noise per cell per frame gives every particle an
    independent life, which at any frame rate reads as static; sampling one
    fixed lattice at a moving offset makes the pattern move *through* the
    cells, so the eye tracks particles across the vessel instead.
    """
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    # smoothstep, so the field has no creases where it crosses a lattice line
    sx = fx * fx * (3 - 2 * fx)
    sy = fy * fy * (3 - 2 * fy)
    top = _particle_noise(ix, iy, seed)
    top += (_particle_noise(ix + 1, iy, seed) - top) * sx
    bot = _particle_noise(ix, iy + 1, seed)
    bot += (_particle_noise(ix + 1, iy + 1, seed) - bot) * sx
    return top + (bot - top) * sy


def _v_half_width(y: float, shoulder: float, basin: float) -> float:
    """The user's vessel: roof, outward shoulder, long taper, flat basin."""
    cap = shoulder * .76
    if y < .16:
        return cap + (shoulder - cap) * (y / .16)
    u = (y - .16) / .84
    return shoulder + (basin - shoulder) * (u * (.82 + .18 * u))


def _particle_material(state: str, x: float, y: float, t: float, *,
                       u: float, v: float, noise: float, tools: int) -> float:
    """Return particle energy at one cell. Geometry is fixed; only this moves.

    ``x``/``y`` are the vessel's own coordinates — x is -1..1 across the
    silhouette at that height, y is 0 at the roof and 1 at the basin — so the
    slow, shaped part of each state is written in terms of the vessel. ``u``
    and ``v`` are the cell's offset in *character* cells from the centre of
    the top row, which is what the drifting noise is sampled in: a texture
    advected in vessel space would stretch as the V narrows and the drift
    would visibly speed up towards the basin.
    """
    if state == "muted":
        return .22 + noise * .19
    if state == "idle":
        # Convection. Particles rise out of the basin and thin out at the
        # shoulders, around an ember that wanders slowly along the floor.
        lift = _flow(u * .52, v * .70 - t * .80, 3)
        swirl = _flow(u * .30 + math.sin(t * .27) * .7, v * .38 - t * .33, 9)
        breath = .5 + .5 * math.sin(t * 1.05 - y * 1.5)
        ember = math.exp(-((x - math.sin(t * .43) * .34) ** 2 * 4.2
                           + (y - .68) ** 2 * 6.5))
        return .13 + lift * .30 + swirl * .17 + breath * .07 + ember * .29
    if state == "wake":
        # Ignition: a flash at the core, then one ring leaving through the
        # shoulders. Timed so a whole one fits inside the 550ms `Tui` holds
        # this state for — a wake that is cut off mid-ring reads as a glitch.
        phase = (t * 1.8) % 1.0
        radius = math.sqrt((x * .78) ** 2 + ((y - .55) * 1.30) ** 2)
        ring = math.exp(-((radius - phase * 1.25) ** 2) / .016)
        flash = math.exp(-phase * 5.0) * math.exp(-radius * radius * 1.8)
        spark = _flow(u * .75, v * .75 - t * 2.4, 5)
        return .21 + spark * .24 + ring * .58 + flash * .32
    if state == "listening":
        # Two bands crossing at different speeds, over a texture drifting
        # sideways — energy arriving from outside rather than made inside.
        near = .5 + .5 * math.sin(t * 5.6 - x * 4.2 + y * 2.4)
        far = .5 + .5 * math.sin(t * 3.9 + x * 5.4 + y * 1.1)
        drift = _flow(u * .48 - t * 2.3, v * .66, 7)
        return .15 + max(near, far) * .33 + drift * .26 + (1 - abs(x)) * .07
    if state == "thinking":
        # Folding inward: rings running towards the core, arms turning around
        # it, one arm per tool in flight, and a core that pulses as they land.
        radius = math.sqrt(x * x * 1.15 + (y - .48) ** 2 * 1.9)
        arms = max(2, min(tools + 1, 5))
        fold = .5 + .5 * math.cos(radius * (7.5 + min(tools, 4) * .9)
                                  + t * 4.6)
        arm = .5 + .5 * math.cos(math.atan2(y - .48, x * .9) * arms - t * 2.2)
        core = math.exp(-radius * radius * 3.2) * (.62 + .38
                                                   * math.sin(t * 3.4))
        churn = _flow(u * .58 + t * .85, v * .58 - t * .5, 11)
        return .13 + fold * .26 + arm * .10 + core * .27 + churn * .18
    # speaking — coherent wavefronts leaving the mouth of the vessel, riding
    # a swell at sentence pace. The one state that radiates.
    radius = math.sqrt(x * x + ((y - .42) * 1.35) ** 2)
    wave = .5 + .5 * math.cos(radius * 9.5 - t * 6.4)
    swell = .5 + .5 * math.sin(t * 2.1 - y * 2.6)
    grain = _flow(u * .42, v * .58 - t * 1.4, 13)
    return .27 + wave * .30 + swell * .10 + grain * .19


def aperture(state: str = "idle", t: float = 0.0, w: int = 28, h: int = 9, *,
             tools: int = 1, motion: bool = True, intensity: float = 1.0
             ) -> list:
    """Draw V's ambient vessel for one instant; always return ``h`` lines.

    A stable V-shaped cloud is implied by density instead of drawn as a hard
    outline. Its vocabulary is deliberately tiny (dot particles at five
    weights), while state is carried by flow: breath, ignition, response,
    convergence and radiation.
    """
    if state not in AMBIENT:
        state = "idle"
    w, h = max(7, w), max(3, h)
    if not motion:
        t = 0.0
    g = [[(" ", "") for _ in range(w)] for _ in range(h)]
    cx = (w - 1) / 2
    depth = max(3, min(12, h))
    top = max(0, (h - depth) // 2)
    shoulder = min((w - 2) / 2, depth * 1.45 + 2)
    basin = max(2.0, min(shoulder * .42, w * .18))
    seed = AMBIENT_ORDER.index(state) * 17
    gain = max(.25, min(1.25, intensity))

    for row in range(top, top + depth):
        y = (row - top) / max(1, depth - 1)
        half = _v_half_width(y, shoulder, basin)
        left = max(0, int(math.ceil(cx - half)))
        right = min(w - 1, int(math.floor(cx + half)))
        for col in range(left, right + 1):
            x = (col - cx) / max(half, 1.0)
            noise = _particle_noise(col, row, seed)
            energy = _particle_material(state, x, y, t, u=col - cx,
                                        v=row - top, noise=noise,
                                        tools=tools) * gain
            energy -= max(0.0, abs(x) - .82) * .42
            # floor(), unlike int(), keeps sub-threshold negative fractions
            # negative. That distinction creates real air between particles.
            level = math.floor((energy - .28) / .12)
            if level < 0:
                continue
            level = min(len(_PARTICLES) - 1, level)
            tint = (LINE_3 if state == "muted" else
                    DEEP if level == 0 else
                    SEED if level < 3 else
                    LIFT if level == 3 else CORE_HI)
            g[row][col] = (_PARTICLES[level], tint)

    return _grid_lines(g)


# The spec's 21 relative bar heights. Real amplitude on a device with a
# microphone; on this surface there is no microphone, so `energy` is passed in
# by whatever *is* the input — see `Tui`, where it is your own typing. Nothing
# here replays a recording and calls it a level.
_WAVE = (.2, .35, .55, .4, .75, .95, .6, .85, 1, .7, .45,
         .8, 1, .62, .9, .5, .72, .38, .55, .3, .18)
_BLOCKS = "▁▂▃▄▅▆▇█"


def waveform(mode: str = "flat", t: float = 0.0, w: int = 21, *,
             energy: float = 1.0, motion: bool = True) -> str:
    """One row of level bars. `flat` · `live` · `speak` · `off`.

    Flat is not "quiet audio" — it is *no* audio, a hairline at the baseline,
    which is what idle and thinking both are. Off is dimmer still and belongs
    to muted alone.
    """
    # The spec's 21 bars are a *shape*, not a count — resampled to whatever
    # the pane is, so the track fills its width instead of stopping short of
    # the border on a wide one.
    n = max(1, w)
    if mode in ("flat", "off"):
        tint = LINE_3 if mode == "flat" else LINE_2
        return f"{tint}{_BLOCKS[0] * n}{RESET}"
    tint = SEED if mode == "live" else LIFT
    period = 0.9 if mode == "live" else 0.7
    energy = max(0.0, min(1.0, energy))
    bars = []
    for i in range(n):
        base = _WAVE[i * len(_WAVE) // n]
        if motion:
            osc = (1 + math.sin(2 * math.pi * (t - i * 0.038) / period)) / 2
            level = base * (0.18 + 0.82 * osc) * energy
        else:
            level = base * energy
        bars.append(_BLOCKS[max(0, min(7, int(level * 8)))])
    return f"{tint}{''.join(bars)}{RESET}"


# --------------------------------------------------------------------------
# the graph
#
# Same rule as the aperture: `core/graph.py` decides what the shape *is* and
# this decides what it looks like. Nothing below opens a database and nothing
# above picks a colour.
#
# Two resolutions in one picture, which is the trick that makes a graph legible
# in thirty columns. Edges are rasterised into braille, so a cell is not one
# dot but a 2×4 grid of them and a line can travel diagonally instead of
# staircasing. Nodes are drawn on top at cell resolution in the aperture's own
# particle vocabulary, because a node has to be *findable* — a note rendered as
# two braille dots is indistinguishable from a passing edge, and the one thing
# this picture exists to say is "that one, there".

# Braille dot bits, flattened to `(sx & 1) * 4 + (sy & 3)`. The ordering is not
# sequential and never was: U+2800 was laid out for a 2×3 cell with a fourth
# row bolted on afterwards, so rows 0-2 are bits 0-2 and row 3 is bit 6.
_BRAILLE = (0x01, 0x02, 0x04, 0x40,
            0x08, 0x10, 0x20, 0x80)
# Node weights by how connected the note is. Degree, not length: what makes a
# note a landmark in a vault is what points at it.
_HUB = (1, 3, 8)
# Every braille cell, pre-built. The renderer asks for these by mask thousands
# of times a second and `chr(0x2800 + mask)` is not free at that rate.
_BRAILLE_CELLS = tuple(chr(0x2800 + m) for m in range(256))

# --- the camera ------------------------------------------------------------
#
# The layout is still solved once and frozen — nothing below re-runs a force
# simulation, and two notes never change which side of each other they are on.
# What moves is the camera and a hand's breadth of float on each node, and both
# are closed-form functions of `t`: no state, no integration, no drift, and a
# frame at any time can be drawn without having drawn the one before it. That
# is what makes it safe to run this at forty-five frames a second and what
# stops it from *boiling*, which is what a live relaxation does.
#
# The tilt oscillates rather than revolves, on purpose. A continuous rotation
# would be easier and it would throw away the one thing the frozen layout is
# for: after a week you know where Gym is because it has always been in that
# corner, and a picture that slowly turns upside down never earns that.
#
# Every amplitude is a fraction of the *view*, not of layout space. That is not
# tidiness: the first cut of this was written in layout units and was invisible,
# because a character cell is enormous. A sway of four hundredths of a unit is
# four cells on an eighty-column canvas and half a cell in a twenty-six-column
# pane — which is to say, on the pane it was nothing at all. Scaling to the
# view means the picture moves by the same *proportion* of itself at any size,
# which is the thing an eye actually reads.
_SWAY_X, _SWAY_Y = 0.075, 0.045      # elliptical pan, as a fraction of view
_SWAY_RATE_X, _SWAY_RATE_Y = 0.43, 0.31
_BREATHE, _BREATHE_RATE = 0.055, 0.23
_TILT, _TILT_RATE = 0.075, 0.17      # ±4.3°, and never a revolution
# Per-node float. Its phase comes from the node's own position, so each one
# moves on its own beat without a hash and without anything stored.
_BOB, _BOB_RATE = 0.022, 1.6
# A spark running along a lit edge, in laps per second. This is the one thing
# in here that reads as *transfer* rather than as decoration — it says the two
# notes it runs between are both part of what V is doing right now.
_SPARK_RATE = 0.6


def graph(view, *, active: str = "", lit=(), w: int = 28, h: int = 10,
          t: float = 0.0, motion: bool = True) -> list[str]:
    """Draw a slice of the vault for one instant; always return ``h`` lines.

    `active` is the note being read *now*, and there is at most one. `lit` is
    the route behind it, most recent first — weight falls off along that list
    rather than with a clock, so what you see is the order the run actually
    visited things and not a decay animation laid over the top of it. A run
    that reads four notes in forty milliseconds draws four notes at four
    weights, immediately. That is what happened.

    With `motion`, the picture is alive whether or not anything is happening:
    the field sways, breathes and tilts, every node floats on its own beat, and
    a spark runs along the edges between notes on the route. All of it is a
    function of `t` alone — see the camera constants above for why that matters
    more than it sounds.
    """
    w, h = max(6, w), max(3, h)
    nodes = getattr(view, "nodes", None)
    if not nodes:
        return [""] * h

    lit = list(lit)
    rank = {rel: i for i, rel in enumerate(lit)}
    warm = set(lit)
    if active:
        warm.add(active)

    # --- the camera, once per frame -------------------------------------
    x0, y0, x1, y1 = view.x0, view.y0, view.x1, view.y1
    spanx = (x1 - x0) or 1.0
    spany = (y1 - y0) or 1.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if motion:
        zoom = 1.0 + _BREATHE * math.sin(t * _BREATHE_RATE)
        ang = _TILT * math.sin(t * _TILT_RATE)
        ca, sa = math.cos(ang) * zoom, math.sin(ang) * zoom
        panx = _SWAY_X * spanx * math.sin(t * _SWAY_RATE_X)
        pany = _SWAY_Y * spany * math.cos(t * _SWAY_RATE_Y)
        bob_t = t * _BOB_RATE
        bobx, boby = _BOB * spanx, _BOB * spany
    else:
        ca, sa, panx, pany = 1.0, 0.0, 0.0, 0.0

    # Layout units → *sub-pixels*, folded into two multiplies so the inner loop
    # has no divisions in it. `unit()` says the same thing and says it through
    # a method call per node, which at nine hundred nodes and forty-five frames
    # a second is forty thousand of them for arithmetic this short.
    #
    # Sub-pixels and not cells, because that is what makes the web glide. Cell
    # centres would mean an edge only moves when one of its notes crosses a
    # whole character — so the nodes would twinkle from cell to cell while the
    # lines between them jumped in eight-pixel steps. Braille already carries
    # 2×4 inside a cell; rasterising from sub-pixel endpoints spends it.
    kx = (w - 1) / spanx
    ky = (h - 1) / spany
    last_sx, last_sy = w * 2 - 1, h * 4 - 1

    n_cells = w * h
    # Who owns a cell when two nodes want it. Priority is active, then route
    # order, then degree — a hub is worth keeping over a leaf, but nothing is
    # worth keeping over the note V has open, which is the only cell in here
    # anybody is looking for.
    owner: list = [None] * n_cells
    score: list = [0] * n_cells
    at: dict = {}
    for node in nodes:
        px = node.x - cx + panx
        py = node.y - cy + pany
        if motion:
            # Phase off the node's own position: no hash, no stored field, and
            # two notes in the same place float together, which is right —
            # they are drawn in the same cell anyway.
            ph = px * 8.7 + py * 13.3
            px += bobx * math.sin(bob_t + ph)
            py += boby * math.cos(bob_t * 0.83 + ph)
        rx = px * ca - py * sa + cx
        ry = px * sa + py * ca + cy

        # Flipped on y: layout space has +y up, a terminal has +y down.
        sx = int((rx - x0) * kx * 2.0 + 1.0)
        sy = int((y1 - ry) * ky * 4.0 + 2.0)
        if sx < 0 or sx > last_sx or sy < 0 or sy > last_sy:
            # Drifted out of frame. Dropped rather than clamped: clamping piles
            # everything that left onto the border in a line, which reads as a
            # feature of the vault and is a feature of the arithmetic. The
            # framing margins in `graph.py` are sized so this stays rare.
            continue

        rel = node.rel
        idx = (sy >> 2) * w + (sx >> 1)
        at[rel] = (sx, sy)
        if rel == active:
            weight = 10_000
        elif rel in rank:
            weight = 5_000 - rank[rel]
        else:
            weight = node.deg if node.deg < 1_000 else 999
        if owner[idx] is None or weight > score[idx]:
            owner[idx], score[idx] = node, weight

    # --- edges, into the braille layer, so a node always draws over them --
    bits = bytearray(n_cells)
    hot = bytearray(n_cells)
    spark = bytearray(n_cells)
    phase = (t * _SPARK_RATE) % 1.0 if motion else -1.0
    for a, b in view.edges:
        ia = at.get(a)
        ib = at.get(b)
        if ia is None or ib is None or ia == ib:
            continue
        live = a in warm and b in warm
        ax, ay = ia
        bx, by = ib

        # Bresenham, inlined. As a generator this was the single most expensive
        # thing in the frame — two hundred thousand yields a second, for four
        # integer adds each.
        dx = bx - ax
        dy = by - ay
        if dx < 0:
            dx, stepx = -dx, -1
        else:
            stepx = 1
        if dy < 0:
            dy, stepy = -dy, -1
        else:
            stepy = 1
        steps = dx if dx > dy else dy
        at_spark = int(steps * phase) if live and phase >= 0.0 else -1
        err = dx - dy
        sx, sy = ax, ay
        i = 0
        while True:
            idx = (sy >> 2) * w + (sx >> 1)
            if owner[idx] is None:            # a node owns its cell outright
                bits[idx] |= _BRAILLE[(sx & 1) * 4 + (sy & 3)]
                if live:
                    hot[idx] = 1
                    if i == at_spark:
                        spark[idx] = 1
            if i >= steps:
                break
            i += 1
            e2 = err * 2
            if e2 > -dy:
                err -= dy
                sx += stepx
            if e2 < dx:
                err += dx
                sy += stepy

    # --- compose ---------------------------------------------------------
    # The active node breathes, in step with the sway rather than against it.
    hot_mark = (not motion) or math.sin(t * 4.2) > -0.3
    out = []
    braille = _BRAILLE_CELLS
    for row in range(h):
        parts = []
        last = None
        base = row * w
        for col in range(w):
            idx = base + col
            node = owner[idx]
            if node is not None:
                rel = node.rel
                if rel == active:
                    char, tint = "●", (CORE_HI if hot_mark else LIFT)
                elif rel in rank:
                    # Four steps down the route and then flat. Past that the
                    # ranks are separated by less than a colour step, and
                    # pretending otherwise only makes the early ones dimmer
                    # than they deserve.
                    step = rank[rel]
                    if step > 3:
                        step = 3
                    char = "●" if step < 2 else "•"
                    tint = (CORE_HI, LIFT, SEED, SEED)[step]
                elif node.deg >= _HUB[2]:
                    char, tint = "•", SEED
                elif node.deg >= _HUB[1]:
                    char, tint = "·", DEEP
                elif node.deg >= _HUB[0]:
                    char, tint = "·", LINE_3
                else:
                    char, tint = ".", LINE_3
            elif bits[idx]:
                char = braille[bits[idx]]
                tint = (LIFT if spark[idx] else DEEP) if hot[idx] else LINE_3
            else:
                char, tint = " ", ""
            if tint != last:
                parts.append(tint or RESET)
                last = tint
            parts.append(char)
        parts.append(RESET)
        out.append("".join(parts))
    return out


def graph_caption(rel: str, w: int) -> str:
    """The note under the mark: its folder, quietly, then its name.

    The folder number is an ordering device for the file manager and carries
    nothing here — `Work` says what `02 - Work` says, in three fewer columns
    out of a budget of twenty-eight.
    """
    if not rel:
        return ""
    base = rel.rsplit("/", 1)[-1]
    if base.endswith(".md"):
        base = base[:-3]
    top = rel.split("/", 1)[0] if "/" in rel else ""
    if top[:2].isdigit() and " - " in top:
        top = top.split(" - ", 1)[1]
    if not top or cell_width(top) + 2 >= w:
        return f"{PAPER}{truncate(base, w)}{RESET}"
    room = w - cell_width(top) - 1
    return f"{DIM3}{top}{RESET} {PAPER}{truncate(base, room)}{RESET}"


# --------------------------------------------------------------------------
# clipboard


# The clipboard tools, each with the flags for the two selections X has: the
# clipboard proper, and PRIMARY — the one a drag fills and middle-click pastes.
_CLIPBOARD = (
    (["wl-copy"], ["wl-copy", "--primary"]),
    (["xclip", "-selection", "clipboard"], ["xclip", "-selection", "primary"]),
    (["xsel", "--clipboard", "--input"], ["xsel", "--primary", "--input"]),
    (["pbcopy"], None),
)


def copy(text: str, primary: bool = False) -> str | None:
    """Put `text` on the system clipboard. Returns how, or None if it couldn't.

    Tries the real clipboard tools first, then falls back to OSC 52, which the
    terminal itself handles — that one works over SSH, where wl-copy is talking
    to a display the text will never reach.

    `primary` also fills the X primary selection, for the window's own drag-to-
    select: taking the mouse means the terminal no longer fills PRIMARY on a
    drag, and middle-click paste would quietly stop working if nothing else
    did it.
    """
    def run(cmd: list[str]) -> bool:
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    for cmd, prim in _CLIPBOARD:
        if not shutil.which(cmd[0]):
            continue
        if not run(cmd):
            continue
        if primary and prim:
            run(prim)               # best effort; the clipboard is the point
        return cmd[0]
    if COLOUR:
        payload = base64.b64encode(text.encode()).decode()
        # `sys.__stdout__`, not `sys.stdout`. The window runs its command
        # handlers under `redirect_stdout` so their printed views can be put
        # in a pane — and an escape sequence written into that capture is
        # rendered as text in the transcript instead of reaching the terminal,
        # so `/copy` with no clipboard tool installed silently did nothing but
        # print itself.
        out = sys.__stdout__ or sys.stdout
        out.write(f"\x1b]52;c;{payload}\x07")
        out.flush()
        return "terminal (OSC 52)"
    return None


# --------------------------------------------------------------------------
# line editing


def setup_readline(history: "os.PathLike | str | None" = None,
                   words: list[str] | None = None,
                   describe: "list[tuple[str, str, str]] | None" = None) -> None:
    """Arrow keys, history, bracketed paste, tab completion.

    Importing readline is most of it — `input()` picks it up automatically —
    but it has to happen before the first prompt, and the caller has to mark
    the prompt's escape codes invisible (see `prompt()`) or readline's idea of
    the cursor column drifts by the length of every colour sequence.
    """
    try:
        import atexit
        import readline
    except ImportError:      # Windows without pyreadline; the REPL still works
        return

    readline.parse_and_bind("tab: complete")
    readline.parse_and_bind("set enable-bracketed-paste on")
    readline.parse_and_bind("set colored-stats off")
    readline.parse_and_bind("set horizontal-scroll-mode off")
    readline.set_completer_delims(" \t\n")

    if words:
        pool = sorted(set(words))

        def complete(text: str, state: int):
            hits = [w for w in pool if w.startswith(text)]
            return hits[state] + " " if state < len(hits) else None

        readline.set_completer(complete)

    if describe:
        # readline's own match list is a bare column of command names, which
        # is the one thing the design says a command list must never be: the
        # spoken phrasing has to sit next to the shortcut, or the shortcut
        # becomes the interface. Replacing the display hook is the only way to
        # get that under `input()` without taking the terminal into raw mode —
        # which would cost select-to-copy, and is not worth a nicer menu.
        def show(substitution, matches, longest):     # noqa: ARG001
            typed = readline.get_line_buffer()
            sys.stdout.write("\n" + palette(describe, typed.strip()) + "\n")
            # The \001/\002 markers are for readline's own width accounting;
            # written by hand they would print as control characters.
            bare = prompt().replace("\001", "").replace("\002", "")
            sys.stdout.write(bare + readline.get_line_buffer())
            sys.stdout.flush()

        try:
            readline.set_completion_display_matches_hook(show)
        except AttributeError:      # libedit; the plain match list still works
            pass

    if history:
        path = str(history)
        try:
            readline.read_history_file(path)
        except (FileNotFoundError, OSError):
            pass
        readline.set_history_length(2000)
        atexit.register(lambda: _save_history(readline, path))


def _save_history(readline, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        readline.write_history_file(path)
    except OSError:
        pass


def prompt(symbol: str = "›") -> str:
    """The input prompt, with its colour codes hidden from readline.

    \\001 … \\002 tell readline "these bytes take no space". Without them every
    left-arrow, every history recall and every line that wraps is off by the
    width of the escape sequences, which is what made the old prompt feel
    broken.
    """
    if not COLOUR:
        return f"{symbol} "
    return f"\001{ACCENT}{BOLD}\002{symbol}\001{RESET}\002 "


# --------------------------------------------------------------------------
# chrome


def rule(label: str = "", cols: int | None = None) -> str:
    cols = cols or width()
    if not label:
        return f"{LINE_2}{'─' * cols}{RESET}"
    bar = "─" * max(cols - len(label) - 3, 0)
    return f"{LINE_2}── {RESET}{DIM1}{label}{RESET} {LINE_2}{bar}{RESET}"


# The aperture, reduced to one cell. The mark is three concentric circles —
# outer ring, dashed ring, core dot — and at this size the dash is the first
# thing to go, exactly as it does at 16px on screen. It is emphatically *not*
# the ✦: that glyph belongs to V's voice, and a logo that borrows it makes
# every line V speaks look like a brand mark.
MARK = "◎"


def banner(name: str, tagline: str) -> str:
    return (f"\n{INDENT}{SEED}{MARK}{RESET} {BOLD}{PAPER_HI}{name}{RESET}  "
            f"{DIM2}{tagline}{RESET}")


def label(text: str) -> str:
    """A section label: mono, spaced, quiet. The spec's `.2em` in a terminal."""
    return f"{INDENT}{DIM1}{' '.join(text.upper())}{RESET}"


def kv(rows: "list[tuple[str, str]]", tint: str = BODY_HI,
       gap: int = 0) -> str:
    """Label-value rows — the session, cost and budget blocks."""
    # Measured off the longest label rather than fixed, so a column never
    # closes up against its value the moment a label grows by one word.
    gap = gap or max((cell_width(k) for k, _ in rows), default=0) + 2
    return "\n".join(f"{HANG}{DIM2}{pad(k, gap)}{RESET}{tint}{v}{RESET}"
                     for k, v in rows)


def boot(version: str, notes: int, shell: str, budget: str = "") -> str:
    """The one line printed before the prompt on a cold start.

    Not a greeting. V does not introduce himself to the person who wrote him,
    so this is machine state and nothing else.
    """
    bits = [f"v {version}", f"{notes} notes indexed", shell]
    if budget:
        bits.append(budget)
    return f"{INDENT}{DIM3}{' — '.join(bits[:1])} — {' · '.join(bits[1:])}{RESET}"


def _marked(sigil: str, text: str, body_style: str = "") -> str:
    """One flagged line, wrapped and hung under its sigil.

    These carry real sentences — a schema error, a "no clipboard tool found"
    — and a status line that runs off the right edge is the thing this module
    exists to stop doing.
    """
    plain = strip_ansi(text)
    lines = _wrap(plain, text, width() + len(INDENT),
                  f"{INDENT}{sigil} {body_style}", f"{INDENT}  {body_style}")
    return "\n".join(ln + RESET for ln in lines)


def hint(text: str) -> str:
    lines = _wrap(strip_ansi(text), text, width() + len(INDENT),
                  INDENT + DIM3, INDENT + DIM3)
    return "\n".join(ln + RESET for ln in lines)


def error(text: str) -> str:
    """Something broke. Amber, not red — red belongs to refusals.

    A refusal is V holding a rule you gave him and is not a fault; an error is
    a fault and is not a decision. Colouring them the same teaches you to read
    one as the other, and the one you learn to ignore is the refusal.
    """
    return _marked(f"{UNSURE}✗{RESET}", text)


def refused(text: str) -> str:
    """V declined. Never styled as an error, and never offers a retry."""
    return _marked(f"{REFUSE}{G_REFUSED}{RESET}", text)


def ok(text: str) -> str:
    return _marked(f"{FREE}✓{RESET}", text)


def note(text: str) -> str:
    return _marked(f"{UNSURE}!{RESET}", text)


def tail(parts: list[str]) -> str:
    """The dim metadata line under an answer: route · timing · what it cost."""
    return f"{HANG}{DIM2}{' · '.join(p for p in parts if p)}{RESET}"


def palette(rows: "list[tuple[str, str, str]]", typed: str = "",
            cols: int | None = None) -> str:
    """The slash commands, each beside the sentence that also works.

    Slash is a hint, not a language. Every command here is a shortcut for
    something V already understands in plain words, and showing that phrasing
    next to it is what stops the shortcut from becoming the only way in. A
    command without a spoken equivalent does not belong in this table.
    """
    cols = cols or width()
    hits = [r for r in rows if r[0].startswith(typed)] if typed else list(rows)
    if not hits:
        return (f"{HANG}{DIM3}no command matches — press enter to send "
                f"“{typed}” as plain language{RESET}")
    # Three columns that have to add up to the pane rather than to whatever
    # they happen to want. The spoken phrasing is the column that earns this
    # table its place, so it is measured first and the description absorbs
    # whatever is left — the other way round pushed it through the border.
    avail = max(30, cols - len(HANG))
    cmd_w = max(cell_width(r[0]) for r in hits) + 2
    spoken_w = min(30, max((cell_width(r[2]) for r in hits), default=0) + 3)
    desc_w = max(12, avail - cmd_w - spoken_w)
    out = []
    for i, (cmd, desc, spoken) in enumerate(hits):
        tint = PAPER_HI if (typed and i == 0) else SEED
        said = f"“{truncate(spoken, spoken_w - 3)}”" if spoken else ""
        out.append(f"{HANG}{tint}{pad(cmd, cmd_w)}{RESET}"
                   f"{BODY}{pad(truncate(desc, desc_w - 2), desc_w)}{RESET}"
                   f"{DIM3}{said}{RESET}")
    return "\n".join(out)


def timeline(steps: "list[tuple[str, str, float, str]]") -> str:
    """The route behind one answer: what ran, where, how long, and why.

    Each row is (name, where, seconds, why). `where` picks the dot colour —
    local is green because it never left the machine, a model is violet
    because it did, held is amber because nothing was written. The *why* is
    the point of the whole view: a list of steps says what happened, and the
    question being asked is why it had to.
    """
    dot = {"local": FREE, "model": SEED, "held": UNSURE}
    out = []
    for i, (name, where, secs, why) in enumerate(steps):
        last = i == len(steps) - 1
        out.append(f"{HANG}{dot.get(where, DIM2)}●{RESET} "
                   f"{PAPER_HI}{name}{RESET}  {DIM3}{secs:.2f}s{RESET}  "
                   f"{DIM2}{where}{RESET}")
        for line in textwrap.wrap(why, width=max(30, width() - 8)) if why else []:
            out.append(f"{HANG}{LINE_3}{'│' if not last else ' '}{RESET}   "
                       f"{BODY}{line}{RESET}")
        if not last:
            out.append(f"{HANG}{LINE_3}│{RESET}")
    return "\n".join(out)
