"""Behavioural tests for the vault graph — the shape, the draw, and the wire.

Run:  python3 -m unittest v.tests.test_graph   (from `src/`)

Three seams, and they are tested apart because they are meant to be apart:
`shells` recognises a path in a stream and knows nothing about the vault,
`graph` turns paths into a shape and knows nothing about colour, and `ui`
turns a shape into cells and knows nothing about where it came from.
"""

import io
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from v.core import graph, shells, tui, ui

TODAY = "01 - Daily Notes/2026/08 - August/2026-08-22.md"
SPRINT = "02 - Work/Sprints/Sprint 42.md"
MR = "02 - Work/Merge Requests/MR-1411 feat(quality).md"


def a_graph(rels=(TODAY, SPRINT, MR), edges=((TODAY, SPRINT), (SPRINT, MR))):
    """A graph built by hand, laid out, without going near a database."""
    g = graph.Graph()
    for rel in rels:
        g.nodes[rel] = graph.Node(rel=rel, title=graph._stem(rel))
        g._by_stem.setdefault(graph._stem(rel).lower(), []).append(rel)
    for a, b in edges:
        key = (a, b) if a < b else (b, a)
        g.edges.append(key)
        g.adj.setdefault(a, set()).add(b)
        g.adj.setdefault(b, set()).add(a)
    for rel, node in g.nodes.items():
        node.deg = len(g.adj.get(rel, ()))
    for rel, (x, y) in graph._relax(list(g.nodes), g.edges, g.adj).items():
        g.nodes[rel].x, g.nodes[rel].y = x, y
    return g


# ---------------------------------------------------------------------------
# the wire: a path in the stream


class PathsInTheStreamTests(unittest.TestCase):
    """`shells` has to hand over the note, not a sentence mentioning it."""

    def test_a_vault_tool_names_its_note_in_the_argument(self):
        self.assertEqual(shells._input_rel({"rel": TODAY}), TODAY)

    def test_a_host_read_names_it_absolutely(self):
        self.assertEqual(
            shells._input_rel({"file_path": f"/home/x/Documents/Brain/{TODAY}"}),
            TODAY)

    def test_the_detail_line_now_survives_a_host_read(self):
        """`file_path` was missing from the key list, so a plain `Read` showed
        a bare tool name and no hint of what it was reading."""
        self.assertIn("2026-08-22",
                      shells._tool_detail({"file_path": f"/x/{TODAY}"}))

    def test_a_call_about_nothing_in_the_vault_names_nothing(self):
        for payload in ({"command": "git status"}, {"file_path": "/etc/hosts"},
                        {"query_text": "pricing refactor"}, None, "nope"):
            self.assertEqual(shells._input_rel(payload), "")

    def test_a_search_result_gives_up_every_note_it_cites(self):
        rels = shells._result_rels(
            f"1. {TODAY} (0.82)\n2. {SPRINT} (0.71)\n3. {MR} (0.66)\n")
        self.assertEqual(rels, [TODAY, SPRINT, MR])

    def test_a_note_cited_twice_is_one_note(self):
        self.assertEqual(shells._result_rels(f"{TODAY}\n{TODAY}\n{SPRINT}"),
                         [TODAY, SPRINT])

    def test_a_result_block_is_read_as_well_as_a_string(self):
        self.assertEqual(
            shells._result_rels([{"type": "text", "text": f"see {SPRINT}"}]),
            [SPRINT])

    def test_a_huge_result_does_not_light_the_whole_vault(self):
        text = "\n".join(f"09 - Anti-Library/note {i}.md" for i in range(80))
        self.assertEqual(len(shells._result_rels(text)),
                         shells._MAX_RESULT_RELS)

    def test_a_path_stops_at_a_quote_and_at_a_pipe(self):
        """The same regex has to survive JSON, a markdown table and a
        wikilink, and in all three something else follows the `.md`."""
        for wrapper in (f'"{SPRINT}"', f"| {SPRINT} |", f"[[{SPRINT}]]"):
            self.assertEqual(shells._result_rels(wrapper), [SPRINT])

    def test_the_event_carries_them_without_a_surface_attached(self):
        ev = shells.ShellEvent("tool", tool="vault_read", rel=TODAY)
        self.assertEqual(ev.rel, TODAY)
        self.assertEqual(shells.ShellEvent("tool_result").rels, [])


class LiveStreamTests(unittest.TestCase):
    """The same, through `_Live.absorb`, which is what actually runs."""

    def absorb(self, *events):
        live, out = shells._Live(), []
        for event in events:
            live.absorb(event, out.append)
        return out

    def test_a_tool_call_arrives_with_its_note(self):
        [ev] = self.absorb({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "vault_read",
                                     "input": {"rel": TODAY}}]}})
        self.assertEqual((ev.kind, ev.rel), ("tool", TODAY))

    def test_a_result_arrives_with_its_constellation(self):
        events = self.absorb(
            {"type": "assistant",
             "message": {"content": [{"type": "tool_use", "name": "vault_search",
                                      "input": {"query_text": "pricing"}}]}},
            {"type": "user",
             "message": {"content": [{"type": "tool_result",
                                      "content": f"{TODAY}\n{SPRINT}"}]}})
        self.assertEqual(events[0].rel, "")          # a search is about a query
        self.assertEqual(events[1].rels, [TODAY, SPRINT])


# ---------------------------------------------------------------------------
# the shape


class ResolutionTests(unittest.TestCase):
    def test_a_full_path_resolves_to_itself(self):
        self.assertEqual(a_graph().resolve(TODAY), TODAY)

    def test_a_wikilink_resolves_by_basename(self):
        """`[[Sprint 42]]` is how a vault actually writes a link, and two
        thirds of them look like this."""
        self.assertEqual(a_graph().resolve("Sprint 42"), SPRINT)

    def test_a_heading_and_an_alias_are_not_part_of_the_name(self):
        for link in ("Sprint 42#Goal", "Sprint 42|the sprint"):
            self.assertEqual(a_graph().resolve(link), SPRINT)

    def test_a_link_to_a_note_that_does_not_exist_resolves_to_nothing(self):
        for miss in ("2026-W99", "", "  ", "nope/at/all.md"):
            self.assertIsNone(a_graph().resolve(miss))


class LayoutTests(unittest.TestCase):
    def test_the_same_vault_lays_out_the_same_way_twice(self):
        """The whole value of the picture is that it can be learned, which it
        cannot be if it reshuffles on restart."""
        first = {n.rel: (n.x, n.y) for n in a_graph().nodes.values()}
        second = {n.rel: (n.x, n.y) for n in a_graph().nodes.values()}
        self.assertEqual(first, second)

    def test_positions_land_inside_the_unit_square(self):
        for node in a_graph().nodes.values():
            self.assertLessEqual(abs(node.x), 1.0 + 1e-6)
            self.assertLessEqual(abs(node.y), 1.0 + 1e-6)

    def test_linked_notes_settle_closer_than_unlinked_ones(self):
        """The one property a force layout is actually for."""
        g = a_graph(rels=(TODAY, SPRINT, MR),
                    edges=((TODAY, SPRINT),))
        def dist(a, b):
            na, nb = g.nodes[a], g.nodes[b]
            return (na.x - nb.x) ** 2 + (na.y - nb.y) ** 2
        self.assertLess(dist(TODAY, SPRINT), dist(TODAY, MR))

    def test_a_lone_note_does_not_divide_by_zero(self):
        g = a_graph(rels=(TODAY,), edges=())
        view = g.whole()
        self.assertEqual(len(view.nodes), 1)
        ux, uy = view.unit(view.nodes[0])
        self.assertTrue(0.0 <= ux <= 1.0 and 0.0 <= uy <= 1.0)

    def test_an_empty_vault_lays_out_to_nothing(self):
        self.assertEqual(graph._relax([], [], {}), {})


class NeighbourhoodTests(unittest.TestCase):
    def test_the_focus_is_in_it_and_comes_first(self):
        view = a_graph().neighbourhood([SPRINT])
        self.assertEqual(view.nodes[0].rel, SPRINT)

    def test_one_hop_reaches_the_neighbours_and_stops(self):
        g = a_graph(rels=(TODAY, SPRINT, MR),
                    edges=((TODAY, SPRINT), (SPRINT, MR)))
        rels = {n.rel for n in g.neighbourhood([TODAY], hops=1).nodes}
        self.assertEqual(rels, {TODAY, SPRINT})
        self.assertIn(MR, {n.rel for n in
                           g.neighbourhood([TODAY], hops=2).nodes})

    def test_it_never_returns_more_than_it_was_asked_for(self):
        rels = [f"09 - Anti-Library/n{i}.md" for i in range(40)]
        g = a_graph(rels=rels, edges=[(rels[0], r) for r in rels[1:]])
        self.assertEqual(len(g.neighbourhood([rels[0]], limit=9).nodes), 9)

    def test_it_carries_only_edges_between_notes_it_kept(self):
        g = a_graph()
        view = g.neighbourhood([TODAY], hops=1)
        kept = {n.rel for n in view.nodes}
        for a, b in view.edges:
            self.assertIn(a, kept)
            self.assertIn(b, kept)

    def test_a_neighbourhood_is_laid_out_again_and_fills_its_frame(self):
        """Global coordinates would put a cluster in one corner of the pane and
        draw it as a bundle of parallel lines. Re-solved, it spreads."""
        rels = [f"09 - Anti-Library/n{i}.md" for i in range(12)]
        g = a_graph(rels=rels, edges=[(rels[0], r) for r in rels[1:]])
        view = g.neighbourhood([rels[0]], limit=12)
        xs = [view.unit(n)[0] for n in view.nodes]
        ys = [view.unit(n)[1] for n in view.nodes]
        self.assertGreater(max(xs) - min(xs), 0.5)
        self.assertGreater(max(ys) - min(ys), 0.5)

    def test_a_view_does_not_move_the_graph_it_came_from(self):
        g = a_graph()
        before = {n.rel: (n.x, n.y) for n in g.nodes.values()}
        g.neighbourhood([TODAY])
        self.assertEqual({n.rel: (n.x, n.y) for n in g.nodes.values()}, before)

    def test_a_focus_that_is_not_in_the_vault_is_simply_dropped(self):
        self.assertEqual(a_graph().neighbourhood(["nope.md"]).nodes, [])


class LoadingTests(unittest.TestCase):
    """`load()` against a real index, and against every way one can be absent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def settings(self):
        class S:
            state_dir = self.dir
            db_path = self.dir / "assistant.db"
            def ensure_dirs(self):
                pass
        return S()

    def build(self, notes, links):
        conn = sqlite3.connect(self.dir / "assistant.db")
        conn.execute("CREATE TABLE notes (rel TEXT, title TEXT, folder TEXT)")
        conn.execute("CREATE TABLE links (src TEXT, dst TEXT)")
        conn.executemany("INSERT INTO notes VALUES (?,?,?)", notes)
        conn.executemany("INSERT INTO links VALUES (?,?)", links)
        conn.commit()
        conn.close()

    def test_it_reads_notes_and_resolves_short_links(self):
        self.build([(TODAY, "2026-08-22", "01 - Daily Notes"),
                    (SPRINT, "Sprint 42", "02 - Work")],
                   [(TODAY, "Sprint 42")])
        g = graph.load(self.settings())
        self.assertEqual(len(g.nodes), 2)
        self.assertEqual(len(g.edges), 1)
        self.assertEqual(g.nodes[TODAY].deg, 1)

    def test_a_link_to_a_note_that_does_not_exist_is_dropped(self):
        self.build([(TODAY, "2026-08-22", "01 - Daily Notes")],
                   [(TODAY, "2026-W99")])
        self.assertEqual(graph.load(self.settings()).edges, [])

    def test_a_note_that_links_to_itself_is_not_an_edge(self):
        self.build([(TODAY, "2026-08-22", "")], [(TODAY, TODAY)])
        self.assertEqual(graph.load(self.settings()).edges, [])

    def test_the_same_pair_linked_both_ways_is_one_edge(self):
        self.build([(TODAY, "a", ""), (SPRINT, "b", "")],
                   [(TODAY, SPRINT), (SPRINT, TODAY)])
        self.assertEqual(len(graph.load(self.settings()).edges), 1)

    def test_no_index_at_all_is_an_empty_graph_and_not_a_crash(self):
        g = graph.load(self.settings())
        self.assertFalse(g)
        self.assertEqual(g.whole().nodes, [])

    def test_a_damaged_index_is_an_empty_graph_and_not_a_crash(self):
        (self.dir / "assistant.db").write_text("this is not a database")
        self.assertFalse(graph.load(self.settings()))

    def test_the_layout_is_cached_and_reused(self):
        self.build([(TODAY, "a", ""), (SPRINT, "b", "")], [(TODAY, SPRINT)])
        first = graph.load(self.settings())
        self.assertTrue((self.dir / "graph-layout.json").exists())
        calls = []
        real = graph._relax
        graph._relax = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            second = graph.load(self.settings())
        finally:
            graph._relax = real
        # The whole-vault layout came off disk. The one call that did happen is
        # a neighbourhood, and only if one was asked for — none was.
        self.assertEqual(calls, [])
        self.assertAlmostEqual(second.nodes[TODAY].x,
                               first.nodes[TODAY].x, places=4)

    def test_a_new_link_invalidates_the_cache(self):
        self.build([(TODAY, "a", ""), (SPRINT, "b", "")], [(TODAY, SPRINT)])
        graph.load(self.settings())
        before = (self.dir / "graph-layout.json").read_text()
        conn = sqlite3.connect(self.dir / "assistant.db")
        conn.execute("INSERT INTO notes VALUES (?,?,?)", (MR, "c", ""))
        conn.execute("INSERT INTO links VALUES (?,?)", (SPRINT, MR))
        conn.commit()
        conn.close()
        graph.load(self.settings())
        self.assertNotEqual((self.dir / "graph-layout.json").read_text(), before)

    def test_editing_prose_does_not_invalidate_the_cache(self):
        """A signature over mtimes would re-solve the vault on every save for a
        picture that came out identical."""
        rels = [TODAY, SPRINT]
        edges = [(TODAY, SPRINT)]
        self.assertEqual(graph._signature(rels, edges),
                         graph._signature(list(reversed(rels)), edges))


# ---------------------------------------------------------------------------
# the drawing


def plain(view, **kw):
    kw.setdefault("motion", False)
    return [ui.strip_ansi(line) for line in ui.graph(view, **kw)]


class GraphRenderTests(unittest.TestCase):
    def setUp(self):
        self.g = a_graph()
        self.view = self.g.whole()

    def test_it_always_returns_the_grid_it_was_asked_for(self):
        for w, h in ((6, 3), (14, 5), (28, 10), (80, 30)):
            with self.subTest(size=(w, h)):
                frame = plain(self.view, w=w, h=h)
                self.assertEqual(len(frame), h)
                self.assertTrue(all(ui.cell_width(line) == w
                                    for line in frame))

    def test_the_vocabulary_stays_terminal_native(self):
        """Node particles are the aperture's own; everything else is braille,
        which is one code block and cannot collide with them."""
        chars = set("".join(plain(self.view, w=40, h=14, active=TODAY,
                                  lit=[TODAY, SPRINT])))
        for char in chars:
            self.assertTrue(char in " .·:•●" or 0x2800 <= ord(char) <= 0x28FF,
                            f"{char!r} is not in the vocabulary")

    def test_an_empty_view_draws_nothing_rather_than_failing(self):
        for view in (None, graph.View()):
            self.assertEqual(plain(view, w=20, h=6), [""] * 6)

    def test_every_node_lands_somewhere(self):
        frame = plain(self.view, w=40, h=14)
        marks = sum(line.count(c) for line in frame for c in ".·•●")
        self.assertGreaterEqual(marks, len(self.view.nodes))

    @unittest.skipUnless(ui.COLOUR, "colour is off in this process")
    def test_the_active_note_is_the_brightest_thing_on_it(self):
        lines = ui.graph(self.view, active=TODAY, w=40, h=14, motion=False)
        self.assertIn(ui.CORE_HI, "".join(lines))

    def test_the_active_note_is_drawn_even_where_a_hub_wants_the_cell(self):
        """Two notes can round to the same cell, and the one V has open must
        win — it is the only cell anybody is looking for."""
        g = a_graph()
        for node in g.nodes.values():           # everything on one spot
            node.x = node.y = 0.0
        # One cell, three notes. Whichever one is active has to be the ● in it.
        for active in (TODAY, SPRINT, MR):
            with self.subTest(active=active):
                marked = plain(g.whole(), w=9, h=5, active=active)
                self.assertEqual("".join(marked).count("●"), 1)

    def test_the_route_is_drawn_in_the_order_it_happened(self):
        """Weight falls off along the trail, not with a clock — so the picture
        says which note was read first without waiting for anything.

        Read at the glyph level rather than the colour, so it holds in a
        terminal with no colour at all: the first two steps of a route are ●
        and the rest are •, and reversing the route moves which is which.
        """
        one = plain(self.view, lit=[TODAY, SPRINT, MR], w=40, h=14)
        two = plain(self.view, lit=[MR, SPRINT, TODAY], w=40, h=14)
        self.assertNotEqual(one, two)

    def test_the_picture_is_time_invariant_with_motion_off(self):
        self.assertEqual(plain(self.view, w=30, h=10, active=TODAY, t=0.0),
                         plain(self.view, w=30, h=10, active=TODAY, t=7.3))

    def test_the_picture_is_alive_with_nothing_happening(self):
        """The field sways, breathes and tilts whether or not V is doing
        anything — there is no active note and no route in this one."""
        rest = [ui.strip_ansi(x) for x in
                ui.graph(self.view, w=40, h=14, t=0.0)]
        later = [ui.strip_ansi(x) for x in
                 ui.graph(self.view, w=40, h=14, t=2.0)]
        self.assertNotEqual(rest, later)

    def test_the_motion_is_a_drift_and_not_a_jump(self):
        """One frame to the next at forty-five a second has to be a nudge. A
        picture that reshuffles between frames reads as a fault, not a float."""
        def cells(t):
            return "".join(ui.strip_ansi(x) for x in
                           ui.graph(self.view, w=40, h=14, t=t))
        base = cells(4.0)
        near = sum(a != b for a, b in zip(base, cells(4.0 + 1 / 45)))
        far = sum(a != b for a, b in zip(base, cells(4.0 + 2.0)))
        self.assertLess(near, len(base) * 0.12)
        self.assertGreater(far, near)

    def test_two_notes_never_cross_each_other(self):
        """The layout is still frozen: what moves is the camera. Two notes that
        never swap sides is the whole difference between a drift and a live
        relaxation, which boils — the graph you looked at a second ago has to
        still be the graph in front of you."""
        left = graph.Node(rel=TODAY, deg=1, x=-0.6, y=0.0)
        right = graph.Node(rel=SPRINT, deg=1, x=0.6, y=0.0)
        view = graph.View(nodes=[left, right], edges=[],
                          x0=-1.4, y0=-1.4, x1=1.4, y1=1.4)
        def column(frame, glyph):
            for line in frame:
                at = line.find(glyph)
                if at >= 0:
                    return at
            return -1

        for step in range(160):
            frame = plain(view, w=44, h=15, active=TODAY,
                          t=step * 0.25, motion=True)
            # By column, not by position in the joined text — they bob
            # independently, so they are often on different rows.
            mark, other = column(frame, "●"), column(frame, "·")
            if mark < 0 or other < 0:
                continue                     # one of them swung out of frame
            self.assertLess(mark, other, f"they crossed at t={step * 0.25}")

    def test_a_note_that_drifts_out_of_frame_is_dropped_not_flattened(self):
        """Clamping piles everything that left onto the border in a line, which
        reads as a feature of the vault and is a feature of the arithmetic."""
        # Hard against the corner of the frame. Every amplitude is a fraction
        # of the *view*, so a note at the centre can never be swung out of one
        # — it is the edge of the picture that this is about.
        node = graph.Node(rel=TODAY, deg=1, x=0.97, y=0.97)
        tight = graph.View(nodes=[node], edges=[],
                           x0=-1.0, y0=-1.0, x1=1.0, y1=1.0)
        drawn = {sum(ui.strip_ansi(r).count("·") for r in
                     ui.graph(tight, w=20, h=7, t=i * 0.3))
                 for i in range(80)}
        self.assertIn(0, drawn)              # it leaves
        self.assertIn(1, drawn)              # and comes back
        self.assertEqual(drawn, {0, 1})      # and is never in two places

    def test_the_spark_runs_along_a_lit_edge(self):
        """The one thing in here that reads as transfer rather than as
        decoration: both notes it runs between are on the route."""
        pair = a_graph(rels=(TODAY, SPRINT), edges=((TODAY, SPRINT),))
        seen = {"".join(ui.graph(pair.whole(), w=34, h=9, lit=[TODAY, SPRINT],
                                 t=i * 0.09))
                for i in range(24)}
        self.assertGreater(len(seen), 1)

    @unittest.skipUnless(ui.COLOUR, "colour is off in this process")
    def test_an_unlit_edge_never_sparks(self):
        pair = a_graph(rels=(TODAY, SPRINT), edges=((TODAY, SPRINT),))
        for i in range(24):
            self.assertNotIn(ui.LIFT, "".join(
                ui.graph(pair.whole(), w=34, h=9, t=i * 0.09)))

    @unittest.skipUnless(ui.COLOUR, "colour is off in this process")
    def test_an_edge_between_two_lit_notes_is_lit_too(self):
        """The route is a path, not a set of points — the line joining two
        notes V just walked is part of what it walked."""
        cold = ui.graph(self.view, w=40, h=14, motion=False)
        warm = ui.graph(self.view, w=40, h=14, lit=[TODAY, SPRINT],
                        active=TODAY, motion=False)
        self.assertNotEqual(cold, warm)
        # …and the difference is not only the two node cells. Two notes, one
        # edge, both of degree one — so every node cell is drawn in LINE_3 and
        # the only thing that can be DEEP is the line between them.
        pair = a_graph(rels=(TODAY, SPRINT), edges=((TODAY, SPRINT),))
        view = pair.whole()
        self.assertNotIn(ui.DEEP, "".join(
            ui.graph(view, w=30, h=9, motion=False)))
        self.assertIn(ui.DEEP, "".join(
            ui.graph(view, w=30, h=9, lit=[TODAY, SPRINT], motion=False)))

    def test_a_caption_names_the_note_without_its_folder_number(self):
        cap = ui.strip_ansi(ui.graph_caption(TODAY, 28))
        self.assertIn("2026-08-22", cap)
        self.assertNotIn("01 - ", cap)
        self.assertLessEqual(ui.cell_width(cap), 28)

    def test_a_caption_for_nothing_is_nothing(self):
        self.assertEqual(ui.graph_caption("", 28), "")

    def test_a_caption_narrower_than_its_folder_keeps_the_name(self):
        cap = ui.strip_ansi(ui.graph_caption(MR, 12))
        self.assertLessEqual(ui.cell_width(cap), 12)
        self.assertTrue(cap.strip())


# ---------------------------------------------------------------------------
# the window


class CellWidthFastPathTests(unittest.TestCase):
    """`cell_width` grew a fast path under the graph, and it is under the whole
    window — every `pad` and every `clip` of every frame goes through it. The
    only thing that matters about it is that it never disagrees with the slow
    path it is skipping."""

    @staticmethod
    def slow(text: str) -> int:
        import unicodedata
        w = 0
        for ch in unicodedata.normalize("NFC", ui.strip_ansi(text)):
            if unicodedata.combining(ch) or ch in "​‌‍‎‏﻿⁦⁧⁨⁩‪‫‬‭‮":
                continue
            w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return w

    def test_it_agrees_with_the_slow_path_on_what_this_window_draws(self):
        for text in ("", "   ", "hello", "Sprint 42 — MR-1411",
                     "⠋⠙⠹⠸ ●•·. ▁▂▃ ━━━ ▮▮▮", "│ ├─╮ ╰──╯",
                     "\x1b[38;5;99mtinted\x1b[0m", "café", "école",
                     "سلام‌دنیا hello", "日本語テスト", "🎉 emoji",
                     "mixed ⠿ سلام 日本 ●"):
            with self.subTest(text=text):
                self.assertEqual(ui.cell_width(text), self.slow(text))

    def test_a_persian_line_still_takes_the_slow_path_and_is_still_right(self):
        """ZWNJ draws nothing and is counted as nothing — which is the whole
        reason this function is not `len()`."""
        self.assertEqual(ui.cell_width("سلام‌دنیا"), 8)

    def test_a_braille_frame_is_measured_by_counting_it(self):
        g = a_graph()
        for line in ui.graph(g.whole(), w=30, h=9, motion=False):
            self.assertEqual(ui.cell_width(line), 30)
            self.assertEqual(ui.cell_width(line), self.slow(line))


class WindowGraphTests(unittest.TestCase):
    COLS, ROWS = 110, 34

    def window(self):
        w = tui.Tui(handle=lambda line: [], commands=[],
                    panes=tui.Panes(model="local", notes=3), version="test")
        w._size = lambda: (self.COLS, self.ROWS)
        w._out = io.StringIO()
        w._graph = a_graph()
        return w

    def test_a_tool_call_moves_the_mark(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        self.assertEqual(w._active, TODAY)
        self.assertEqual(w._trail, [TODAY])

    def test_the_route_is_kept_in_the_order_it_happened(self):
        w = self.window()
        for rel in (TODAY, SPRINT, MR):
            w.progress(shells.ShellEvent("tool", tool="vault_read", rel=rel))
        self.assertEqual(w._trail, [MR, SPRINT, TODAY])
        self.assertEqual(w._active, MR)

    def test_a_note_read_twice_moves_up_rather_than_appearing_twice(self):
        w = self.window()
        for rel in (TODAY, SPRINT, TODAY):
            w.progress(shells.ShellEvent("tool", tool="vault_read", rel=rel))
        self.assertEqual(w._trail, [TODAY, SPRINT])

    def test_a_result_lights_notes_without_taking_the_mark(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w.progress(shells.ShellEvent("tool_result", rels=[SPRINT, MR]))
        self.assertEqual(w._active, TODAY)
        self.assertEqual(set(w._trail), {TODAY, SPRINT, MR})

    def test_the_route_never_grows_without_bound(self):
        w = self.window()
        rels = [f"09 - Anti-Library/n{i}.md" for i in range(40)]
        w._graph = a_graph(rels=rels, edges=())
        for rel in rels:
            w.progress(shells.ShellEvent("tool", tool="vault_read", rel=rel))
        self.assertEqual(len(w._trail), tui.TRAIL_MAX)

    def test_a_path_that_is_not_a_note_is_dropped_quietly(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="Bash", rel=""))
        w.progress(shells.ShellEvent("tool", tool="Read",
                                     rel="09 - Anti-Library/nope.md"))
        self.assertEqual(w._trail, [])

    def test_writing_the_answer_puts_the_mark_out_but_keeps_the_route(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w.progress(shells.ShellEvent("text", text="Today you "))
        self.assertEqual(w._active, "")
        self.assertEqual(w._trail, [TODAY])

    def test_nothing_happens_before_the_graph_has_loaded(self):
        w = self.window()
        w._graph = None
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        self.assertEqual(w._trail, [])

    def test_the_pane_keeps_its_width_with_a_graph_in_it(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        for line in w._side(self.ROWS - 4):
            self.assertLessEqual(ui.cell_width(ui.strip_ansi(line)),
                                 tui.SIDE_W)

    def test_nothing_in_the_pane_moves_when_v_picks_up_a_note(self):
        """The slot is held whether or not anything has been read. Sizing it
        to the route would reflow the pane every time a call went out."""
        w = self.window()
        w._side(self.ROWS - 4)
        before = (w._stage_h, w._graph_h, w._graph_off)
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w._side(self.ROWS - 4)
        self.assertEqual((w._stage_h, w._graph_h, w._graph_off), before)
        self.assertGreater(w._graph_h, 0)

    def test_a_short_pane_drops_the_graph_and_keeps_the_numbers(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        rows = w._side(20)
        self.assertEqual(w._graph_h, 0)
        self.assertIn("S E S S I O N",
                      "\n".join(ui.strip_ansi(r) for r in rows))

    def test_a_pane_too_short_for_the_aperture_has_no_graph_either(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w._side(14)
        self.assertEqual((w._stage_h, w._graph_h), (0, 0))

    def test_the_vault_is_in_the_pane_before_anything_is_read(self):
        w = self.window()
        rows = w._side(self.ROWS - 4)
        self.assertGreater(w._graph_h, 0)
        self.assertIn("G R A P H",
                      "\n".join(ui.strip_ansi(r) for r in rows))

    def test_at_rest_the_caption_names_nothing_because_nothing_is_open(self):
        w = self.window()
        rows = w._side(self.ROWS - 4)
        caption = ui.strip_ansi(rows[w._graph_off + w._graph_h - 1])
        self.assertEqual(caption.strip(), "")

    def test_a_new_question_clears_the_last_answer_s_route(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w._turn("what did I do yesterday")
        self.assertEqual(w._trail, [])
        self.assertEqual(w._active, "")

    def test_the_slice_is_rebuilt_only_when_the_focus_moves(self):
        """It is asked for thirty times a second and the relaxation behind it
        is not free."""
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        first = w._slice()
        self.assertIs(w._slice(), first)
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=SPRINT))
        self.assertIsNot(w._slice(), first)


class CanvasTests(unittest.TestCase):
    COLS, ROWS = 110, 34

    def window(self):
        w = tui.Tui(handle=lambda line: [], commands=[],
                    panes=tui.Panes(model="local", notes=3), version="test")
        w._size = lambda: (self.COLS, self.ROWS)
        w._out = io.StringIO()
        w._graph = a_graph()
        return w

    def test_slash_graph_toggles_it(self):
        w = self.window()
        self.assertTrue(w._window_command("/graph"))
        self.assertTrue(w.canvas)
        w._window_command("/graph")
        self.assertFalse(w.canvas)

    def test_the_canvas_fills_the_transcript_pane_exactly(self):
        w = self.window()
        w.canvas = True
        rows = w._canvas(80, 24)
        self.assertEqual(len(rows), 24)
        for line in rows:
            self.assertLessEqual(ui.cell_width(ui.strip_ansi(line)), 80)

    def test_the_canvas_prints_the_route_under_the_picture(self):
        w = self.window()
        w.canvas = True
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        text = "\n".join(ui.strip_ansi(r) for r in w._canvas(80, 24))
        self.assertIn("2026-08-22", text)
        self.assertIn("notes", text)

    def test_the_canvas_survives_a_vault_it_has_not_loaded(self):
        w = self.window()
        w._graph, w.canvas = None, True
        self.assertEqual(w._canvas(60, 12), [""] * 12)

    def test_the_side_pane_does_not_draw_the_graph_twice(self):
        """Both slices come out of one cache, so drawing both would re-solve a
        neighbourhood on every frame."""
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w.canvas = True
        w._side(self.ROWS - 4)
        self.assertEqual(w._graph_h, 0)

    def test_the_canvas_is_a_rectangle_of_its_own(self):
        """It is the transcript pane, whole. Repainting it through `draw()`
        cost five milliseconds and thirteen kilobytes a frame to move a dozen
        particles — the exact thing the rectangle exists to avoid, dodged only
        because this one is bigger than the one first thought of."""
        w = self.window()
        w.canvas = True
        w.draw()
        self.assertIsNotNone(w._canvas_box)
        row0, col0, cw, ch = w._canvas_box
        self.assertEqual((row0, col0), (2, 3))
        self.assertEqual(ch, self.ROWS - 4)
        self.assertTrue(w._repaint_stage())

    def test_the_canvas_writes_only_inside_its_own_rectangle(self):
        w = self.window()
        w.canvas = True
        w.draw()
        rows = set()
        for box in (w._stage_box, w._canvas_box):
            if box:
                rows |= set(range(box[0], box[0] + box[3]))
        w._out = io.StringIO()
        w._repaint_stage()
        for match in re.finditer(r"\x1b\[(\d+);(\d+)H", w._out.getvalue()):
            row = int(match.group(1))
            if row != self.ROWS - 1:             # back to the input line
                self.assertIn(row, rows)

    def test_the_palette_takes_the_canvas_back(self):
        """It is drawn over the foot of the pane, and that is the transcript's
        business, not a rectangle's."""
        w = self.window()
        w.canvas = True
        w.buf = "/"
        w.draw()
        self.assertIsNone(w._canvas_box)
        self.assertFalse(w._repaint_stage())

    def test_the_canvas_animates_even_muted(self):
        """A mute has nothing to say about the graph. Freezing the vault the
        moment the microphone goes off would be saying something about the
        microphone that is not true."""
        w = self.window()
        w.canvas, w.muted = True, True
        self.assertTrue(w._animating())


class FrameClockTests(unittest.TestCase):
    COLS, ROWS = 110, 34

    def window(self):
        w = tui.Tui(handle=lambda line: [], commands=[],
                    panes=tui.Panes(model="local", notes=3), version="test")
        w._size = lambda: (self.COLS, self.ROWS)
        w._out = io.StringIO()
        w._graph = a_graph()
        return w

    def test_a_graph_on_screen_runs_the_clock_faster(self):
        """The aperture's particles read the same at eighteen frames as at
        forty-five. A field panning across a character grid does not: under
        about thirty it stops gliding and starts stepping."""
        w = self.window()
        w._graph = None
        w.draw()
        without = w._tick()
        w._graph = a_graph()
        w.draw()
        self.assertLess(w._tick(), without)
        self.assertLessEqual(w._tick(), 1 / 45 + 1e-9)

    def test_the_canvas_keeps_up_with_the_pane(self):
        """Four times the rectangle and the same clock, because the aperture
        is not redrawn on every one of its frames."""
        w = self.window()
        w.canvas = True
        w.draw()
        self.assertLessEqual(w._tick(), 1 / 45 + 1e-9)

    def test_the_aperture_keeps_its_own_clock_behind_the_canvas(self):
        """It is a breathing pattern that reads the same at eighteen frames as
        at forty-five, in the corner of a window whose left half is a moving
        graph. Rendering it anyway was a third of the frame."""
        w = self.window()
        w.canvas = True
        real = tui.time.time
        drawn = []
        try:
            now = [500.0]
            tui.time.time = lambda: now[0]
            w.draw()
            original = w._stage
            w._stage = lambda h: (drawn.append(now[0]), original(h))[1]
            for _ in range(45):              # one second at forty-five frames
                now[0] += 1 / 45
                w._repaint_stage()
        finally:
            tui.time.time = real
        self.assertLessEqual(len(drawn), 20)
        self.assertGreaterEqual(len(drawn), 15)

    def test_the_aperture_is_not_throttled_when_it_is_the_only_thing_moving(self):
        w = self.window()
        real = tui.time.time
        drawn = []
        try:
            now = [500.0]
            tui.time.time = lambda: now[0]
            w.draw()
            original = w._stage
            w._stage = lambda h: (drawn.append(now[0]), original(h))[1]
            for _ in range(20):
                now[0] += 1 / 45
                w._repaint_stage()
        finally:
            tui.time.time = real
        self.assertEqual(len(drawn), 20)

    def test_a_still_picture_costs_no_bytes(self):
        """Row-diffed. The camera moves most rows most of the time, but a
        caption and a route do not, and at forty-five frames a second those
        are the bytes that make a slow link feel heavy."""
        w = self.window()
        real = tui.time.time
        try:
            tui.time.time = lambda: 12.5
            w.draw()
            w._out = io.StringIO()
            self.assertTrue(w._repaint_stage())      # primes the row cache
            w._out = io.StringIO()
            self.assertTrue(w._repaint_stage())      # same instant, no change
            self.assertEqual(w._out.getvalue(), "")
        finally:
            tui.time.time = real

    def test_a_full_frame_forgets_what_it_had_painted(self):
        """`draw()` rewrites the screen, so nothing on it can be skipped as
        unchanged afterwards."""
        w = self.window()
        real = tui.time.time
        try:
            tui.time.time = lambda: 12.5
            w.draw()
            w._repaint_stage()
            self.assertTrue(w._painted)
            w.draw()
            self.assertFalse(w._painted)
        finally:
            tui.time.time = real

    def test_reduced_motion_still_stops_everything(self):
        w = self.window()
        w.draw()
        os.environ["V_NO_ANIM"] = "1"
        try:
            self.assertFalse(w._animating())
            self.assertEqual(w._tick(), 0.25)
        finally:
            del os.environ["V_NO_ANIM"]


class PartialRepaintTests(unittest.TestCase):
    """The graph is a second rectangle, and it has to behave like the first."""

    COLS, ROWS = 110, 34

    def window(self):
        w = tui.Tui(handle=lambda line: [], commands=[],
                    panes=tui.Panes(model="local", notes=3), version="test")
        w._size = lambda: (self.COLS, self.ROWS)
        w._out = io.StringIO()
        w._graph = a_graph()
        w.log.append(tui.Entry("plain", "a line of transcript, for width"))
        return w

    def emitted(self, w, call):
        w._out = io.StringIO()
        self.assertIsNot(call(), False)
        return w._out.getvalue()

    def test_a_partial_repaint_matches_a_whole_frame(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        real = tui.time.time
        try:
            tui.time.time = lambda: 4.25
            w.draw()
            self.assertIsNotNone(w._graph_box)
            partial = self.emitted(w, w._repaint_stage)
            whole = self.emitted(w, w.draw)
        finally:
            tui.time.time = real
        # Everything the rectangle wrote has to appear in the whole frame.
        for chunk in re.split(r"\x1b\[\d+;\d+H", partial):
            if chunk.strip() and "\x1b[?25" not in chunk:
                self.assertIn(chunk, whole)

    def test_it_writes_only_inside_the_two_rectangles(self):
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w.draw()
        rows = set()
        for row0, _, _, height in (w._stage_box, w._graph_box):
            rows |= set(range(row0, row0 + height))
        for match in re.finditer(r"\x1b\[(\d+);(\d+)H",
                                 self.emitted(w, w._repaint_stage)):
            row = int(match.group(1))
            if row == self.ROWS - 1:
                continue                         # back to the input line
            self.assertIn(row, rows)

    def test_clearing_the_route_does_not_cost_a_whole_frame(self):
        """The slot is the same size at rest as it is mid-run, which is the
        point of holding it — the end of a turn repaints a rectangle."""
        w = self.window()
        w.progress(shells.ShellEvent("tool", tool="vault_read", rel=TODAY))
        w.draw()
        self.assertIsNotNone(w._graph_box)
        w._trail, w._active = [], ""
        self.assertTrue(w._repaint_stage())

    def test_it_declines_after_a_resize(self):
        w = self.window()
        w.draw()
        w._size = lambda: (self.COLS - 9, self.ROWS - 4)
        self.assertFalse(w._repaint_stage())

    def test_the_stage_alone_still_repaints_on_a_pane_too_short_for_a_graph(self):
        w = self.window()
        w._size = lambda: (self.COLS, 28)
        w.draw()
        self.assertIsNone(w._graph_box)
        self.assertIsNotNone(w._stage_box)
        self.assertTrue(w._repaint_stage())


if __name__ == "__main__":
    unittest.main()
