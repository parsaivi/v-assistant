"""Behavioral tests for the character-cell ambient vessel.

Run:  python3 -m v.tests.test_ui_animation
"""

import io
import re
import unittest

from v.core import tui, ui


def plain_frame(state: str, t: float, width: int = 28, height: int = 9,
                *, motion: bool = True, tools: int = 2) -> list[str]:
    return [ui.strip_ansi(line) for line in ui.aperture(
        state, t, width, height, motion=motion, tools=tools)]


def shape_points(frame: list[str]) -> set[tuple[int, int]]:
    return {(col, row) for row, line in enumerate(frame)
            for col, char in enumerate(line) if char in ".·:•●"}


class AmbientVesselTests(unittest.TestCase):
    def test_every_state_honors_requested_grid(self):
        for width, height in ((7, 3), (13, 5), (28, 7), (40, 11)):
            for state in ui.AMBIENT_ORDER:
                with self.subTest(state=state, size=(width, height)):
                    frame = plain_frame(state, 1.17, width, height)
                    self.assertEqual(len(frame), height)
                    self.assertTrue(all(ui.cell_width(line) == width
                                        for line in frame))

    def test_particle_vocabulary_stays_terminal_native(self):
        allowed = set(" .·:•●")
        for state in ui.AMBIENT_ORDER:
            chars = set("".join(plain_frame(state, 1.17)))
            self.assertLessEqual(chars, allowed)

    def test_geometry_matches_wide_shoulders_and_narrow_base(self):
        # Speaking is dense enough that stochastic feathering cannot obscure
        # the intended silhouette at its shoulders and basin.
        points = shape_points(plain_frame("speaking", 0.0))
        rows = {}
        for x, y in points:
            rows.setdefault(y, []).append(x)
        widths = {y: max(xs) - min(xs) for y, xs in rows.items()}
        top, bottom = min(rows), max(rows)
        self.assertGreater(max(widths.values()), widths[top])
        self.assertGreater(widths[top], widths[bottom])

    def test_particles_have_multiple_visual_weights(self):
        frame = "".join(plain_frame("listening", 1.17, 40, 11))
        self.assertGreaterEqual(len(set(frame) & set(".·:•●")), 4)

    def test_ambient_states_have_distinct_material(self):
        frames = {
            state: "\n".join(plain_frame(state, 1.17, tools=3))
            for state in ui.AMBIENT_ORDER
        }
        self.assertEqual(len(set(frames.values())), len(ui.AMBIENT_ORDER))

    def test_reduced_motion_is_time_invariant(self):
        for state in ui.AMBIENT_ORDER:
            with self.subTest(state=state):
                self.assertEqual(plain_frame(state, 0.0, motion=False),
                                 plain_frame(state, 99.0, motion=False))

    def test_muted_is_time_invariant_even_with_motion_enabled(self):
        self.assertEqual(plain_frame("muted", 0.0),
                         plain_frame("muted", 99.0))

    def test_live_states_actually_animate(self):
        for state in ("idle", "wake", "listening", "thinking", "speaking"):
            with self.subTest(state=state):
                frames = {tuple(plain_frame(state, t))
                          for t in (0.11, 0.41, 0.83, 2.71)}
                self.assertGreater(len(frames), 1)


class Screen:
    """Just enough terminal to check where a frame actually lands.

    Absolute positioning, erase-to-end-of-line, and ONLCR newlines — the
    subset `tui.draw()` and `tui._repaint_stage()` write. Styling is dropped:
    what these tests are about is which character ends up in which cell.
    """

    SEQ = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b\][^\x07]*\x07|\x1b.")

    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.cells = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def write(self, data: str) -> None:
        at = 0
        for m in self.SEQ.finditer(data):
            self._text(data[at:m.start()])
            at = m.end()
            params, final = m.group(1), m.group(2)
            if final == "H":
                nums = [int(p or 1) for p in (params or "").split(";")]
                self.row = (nums[0] if nums else 1) - 1
                self.col = (nums[1] if len(nums) > 1 else 1) - 1
            elif final == "K" and 0 <= self.row < self.rows:
                for c in range(self.col, self.cols):
                    self.cells[self.row][c] = " "
            elif final == "J":
                self.cells = [[" "] * self.cols for _ in range(self.rows)]
        self._text(data[at:])

    def _text(self, text: str) -> None:
        for ch in text:
            if ch == "\n":                       # ONLCR: the CR comes free
                self.row, self.col = self.row + 1, 0
            elif 0 <= self.row < self.rows and 0 <= self.col < self.cols:
                self.cells[self.row][self.col] = ch
                self.col += 1
            else:
                self.col += 1

    def dump(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.cells]


class WindowCase(unittest.TestCase):
    """A window with a fake terminal under it, and the means to read it back."""

    COLS, ROWS = 110, 30

    def window(self) -> "tui.Tui":
        w = tui.Tui(handle=lambda line: ([], None), commands=[],
                    panes=tui.Panes(model="local", notes=12, turns=3),
                    version="test")
        w._size = lambda: (self.COLS, self.ROWS)
        w._out = io.StringIO()
        return w

    def emit(self, w, call) -> str:
        w._out = io.StringIO()
        self.assertIsNot(call(), False)
        return w._out.getvalue()

    def paint(self, w, call, onto: Screen | None = None) -> Screen:
        screen = onto or Screen(self.COLS, self.ROWS)
        screen.write(self.emit(w, call))
        return screen


class FrameTests(WindowCase):
    """The partial repaint has to be indistinguishable from a whole frame.

    It writes by absolute coordinate into a rectangle measured during the
    last full frame, so every one of these is really the same question: does
    it still know where the aperture is?
    """

    def test_partial_repaint_matches_a_whole_frame(self):
        """A frame, then a partial repaint one moment later, must leave the
        screen in exactly the state a whole frame at that moment would."""
        w = self.window()
        w.log.append(tui.Entry("plain", "a line of transcript, for width"))
        real = tui.time.time
        try:
            for state, buf in (("idle", ""), ("listening", "hi")):
                with self.subTest(state=state):
                    w.buf = buf
                    tui.time.time = lambda: 4.25
                    self.assertEqual(w.ambient(), state)
                    screen = self.paint(w, w.draw)
                    tui.time.time = lambda: 9.0
                    self.paint(w, w._repaint_stage, onto=screen)
                    self.assertEqual(screen.dump(),
                                     self.paint(w, w.draw).dump())
        finally:
            tui.time.time = real

    def test_partial_repaint_writes_only_inside_the_stage(self):
        w = self.window()
        w.log.append(tui.Entry("plain", "transcript that must not be touched"))
        screen = self.paint(w, w.draw)
        self.assertIsNotNone(w._stage_box)
        row0, col0, width, height = w._stage_box
        before = screen.dump()
        self.paint(w, w._repaint_stage, onto=screen)
        after = screen.dump()
        for i, (was, now) in enumerate(zip(before, after)):
            if not row0 - 1 <= i < row0 - 1 + height:
                self.assertEqual(was, now, f"row {i} moved")
        # …and inside it, the borders on either side of the pane survive.
        for i in range(row0 - 1, row0 - 1 + height):
            self.assertEqual(after[i][col0 - 3], "│")
            self.assertEqual(after[i][col0 + width], "│")

    def test_partial_repaint_declines_when_it_has_no_measurements(self):
        w = self.window()
        self.assertFalse(w._repaint_stage())     # nothing drawn yet

    def test_partial_repaint_declines_after_a_resize(self):
        w = self.window()
        self.paint(w, w.draw)
        self.assertIsNotNone(w._stage_box)
        w._size = lambda: (self.COLS - 12, self.ROWS - 3)
        self.assertFalse(w._repaint_stage())

    def test_no_stage_on_a_window_too_short_to_hold_one(self):
        w = self.window()
        w._size = lambda: (self.COLS, 14)
        self.paint(w, w.draw)
        self.assertIsNone(w._stage_box)
        self.assertFalse(w._repaint_stage())

    def test_frame_clock_is_smooth_while_animating(self):
        w = self.window()
        for state, cap in (("thinking", 1 / 24), ("speaking", 1 / 24),
                           ("listening", 1 / 24), ("idle", 1 / 12)):
            with self.subTest(state=state):
                w._demo = [state]
                self.assertLessEqual(w._tick(), cap)
        w._demo = None
        w.muted = True                            # muted has no frame clock
        self.assertGreaterEqual(w._tick(), 0.2)


class MouseTests(WindowCase):
    """The wheel, and drag-to-copy.

    Both exist because the terminal's own versions do not survive this
    layout: the wheel arrives as arrow keys on the alternate screen, and a
    native drag copies the border along with the text.
    """

    def loaded(self) -> "tui.Tui":
        w = self.window()
        w.log.append(tui.Entry("plain", "the first line of the transcript"))
        w.log.append(tui.Entry("plain", "and a second line under it"))
        self.paint(w, w.draw)
        return w

    def text_rows(self, w) -> list[int]:
        return [i for i, line in enumerate(w._vis) if ui.strip_ansi(line).strip()]

    def press(self, w, col: int, row: int, b: int = 0, final: str = "M") -> None:
        """One mouse report, in pane coordinates rather than screen ones."""
        pane_row, pane_col, _, _ = w._pane()
        w._mouse(f"\x1b[<{b};{col + pane_col};{row + pane_row}{final}")

    def test_a_mouse_report_is_one_key_and_not_a_line_of_typing(self):
        """The regression that made this whole thing necessary: `_SEQ` did not
        know `<`, so the report came back as a lone Esc and its digits and
        semicolons were typed into the prompt."""
        w = self.window()
        w._pending = "\x1b[<0;5;24M\x1b[<32;20;25M\x1b[<0;20;25m"
        keys = iter(lambda: w._take(), None)
        self.assertEqual(list(keys), ["\x1b[<0;5;24M", "\x1b[<32;20;25M",
                                      "\x1b[<0;20;25m"])

    def test_alternate_scroll_is_turned_off_on_the_way_in(self):
        """Without `?1007l` the wheel is translated into `\x1b[A` by the
        terminal itself, which is indistinguishable from the up arrow."""
        w = self.window()
        w._saved = object()                       # so `_exit` restores nothing
        import termios
        real = (termios.tcgetattr, termios.tcsetattr)
        try:
            termios.tcgetattr = lambda fd: None
            termios.tcsetattr = lambda *a: None
            import tty
            real_cbreak, tty.setcbreak = tty.setcbreak, lambda fd: None
            entered = self.emit(w, lambda: w._enter() or True)
            tty.setcbreak = real_cbreak
            left = self.emit(w, lambda: w._exit() or True)
        finally:
            termios.tcgetattr, termios.tcsetattr = real
        self.assertIn("\x1b[?1007l", entered)
        self.assertIn(tui.MOUSE_ON, entered)
        self.assertIn("\x1b[?1007r", left)       # and put back as it was
        self.assertIn(tui.MOUSE_OFF, left)

    def test_the_wheel_scrolls_the_transcript(self):
        w = self.loaded()
        for i in range(60):                       # more log than pane
            w.log.append(tui.Entry("plain", f"line {i}"))
        self.paint(w, w.draw)
        top = ui.strip_ansi(w._vis[0])
        w._mouse("\x1b[<64;10;10M")               # wheel up
        self.paint(w, w.draw)
        self.assertEqual(w.scroll, 3)
        self.assertNotEqual(ui.strip_ansi(w._vis[0]), top, "nothing moved")
        w._mouse("\x1b[<64;10;10M")
        self.paint(w, w.draw)
        self.assertEqual(w.scroll, 6)
        for _ in range(4):                        # wheel down, past the end
            w._mouse("\x1b[<65;10;10M")
            self.paint(w, w.draw)
        self.assertEqual(w.scroll, 0, "and never past the bottom")
        self.assertEqual(ui.strip_ansi(w._vis[0]), top)

    def test_the_wheel_never_reaches_the_command_history(self):
        w = self.loaded()
        w.history = ["something I said earlier"]
        for report in ("\x1b[<64;10;10M", "\x1b[<65;10;10M"):
            self.assertTrue(w._mouse(report))
        self.assertEqual(w.buf, "")
        self.assertIsNone(w.hist_at)

    def test_a_drag_copies_the_text_without_the_frame(self):
        w = self.loaded()
        grabbed = []
        real, tui.ui.copy = tui.ui.copy, lambda text, primary=False: (
            grabbed.append(text) or "test")
        try:
            first, last = self.text_rows(w)[0], self.text_rows(w)[-1]
            self.press(w, 0, first)
            self.press(w, w._vis_w, last, b=32)
            self.press(w, w._vis_w, last, final="m")
        finally:
            tui.ui.copy = real
        self.assertEqual(len(grabbed), 1)
        self.assertEqual([line.strip() for line in grabbed[0].split("\n")],
                         ["the first line of the transcript",
                          "and a second line under it"])
        for junk in ("│", "BUDGET", "notes"):
            self.assertNotIn(junk, grabbed[0], "the frame came with it")

    def test_a_click_that_selects_nothing_copies_nothing(self):
        w = self.loaded()
        called = []
        real, tui.ui.copy = tui.ui.copy, lambda *a, **k: called.append(a)
        try:
            row = self.text_rows(w)[0]
            self.press(w, 4, row)
            self.press(w, 4, row, final="m")
        finally:
            tui.ui.copy = real
        self.assertEqual(called, [])
        self.assertIsNone(w._sel)

    def test_the_selection_is_lit_without_moving_a_single_cell(self):
        """Reverse video, drawn by walking the escapes already in the line —
        so it must add colour and nothing else. A highlight that charges its
        own escapes as width pushes the row through the border."""
        w = self.loaded()
        # Frozen, or the aperture has drifted a particle between the two
        # frames and the diff is about the animation rather than the
        # highlight.
        real, tui.time.time = tui.time.time, lambda: 4.25
        try:
            before = self.paint(w, w.draw).dump()
            row = self.text_rows(w)[0]
            w._sel = ((row, 0), (row, 12))
            frame = self.emit(w, w.draw)
        finally:
            tui.time.time = real
        self.assertIn(tui.SELECT_ON, frame)
        screen = Screen(self.COLS, self.ROWS)
        screen.write(frame)
        self.assertEqual(screen.dump(), before)

    def test_typing_and_new_answers_both_drop_the_selection(self):
        w = self.loaded()
        row = self.text_rows(w)[0]
        w._sel = ((row, 0), (row, 12))
        w.say(tui.Entry("plain", "something new lands"))
        self.assertIsNone(w._sel, "the rows it was measured against moved")
        w._sel = ((row, 0), (row, 12))
        w._clear_selection()
        self.assertIsNone(w._sel)

    def test_mouse_off_hands_it_back(self):
        w = self.loaded()
        wrote = self.emit(w, lambda: w._window_command("/mouse"))
        self.assertFalse(w.mouse)
        self.assertIn(tui.MOUSE_OFF, wrote)
        w.scroll = 0
        w._mouse("\x1b[<64;10;10M")
        self.assertEqual(w.scroll, 0, "the wheel is the terminal's again")
        wrote = self.emit(w, lambda: w._window_command("/mouse"))
        self.assertTrue(w.mouse)
        self.assertIn(tui.MOUSE_ON, wrote)


class SelectionTextTests(unittest.TestCase):
    def test_columns_are_counted_the_way_they_are_drawn(self):
        """A Persian line is where a slice by index and a slice by column
        disagree: the ZWNJ is a character that occupies no cell."""
        line = "سلام\u200cدنیا hello"
        self.assertEqual(tui._slice_cols(line, 0, 4), "سلام")
        # سلام is four cells, the ZWNJ none, دنیا four, the space one — so
        # `hello` starts at column nine and a slice by index would miss it.
        self.assertEqual(tui._slice_cols(line, 9, 14), "hello")

    def test_highlight_preserves_colour_across_a_reset(self):
        # Literal escapes rather than `ui.PAPER`: the palette is blanked when
        # there is no tty, and this test is about the escapes, not the colour.
        line = "\x1b[38;5;250mred\x1b[0m plain"
        out = tui._highlight(line, 0, 9)
        self.assertIn(ui.PAPER, out)
        # The reset in the middle would otherwise end the inversion early.
        self.assertGreaterEqual(out.count(tui.SELECT_ON), 2)
        self.assertEqual(ui.strip_ansi(out), ui.strip_ansi(line))


if __name__ == "__main__":
    unittest.main()
