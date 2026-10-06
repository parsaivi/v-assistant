"""Tests for the desktop surface (`core/hud.py`).

Run:  python3 -m v.tests.test_hud

Same posture as `test_httpapi.py`: no pytest, no network, no model, and a
throwaway vault where a vault is needed. The engine is constructed exactly
once — in the end-to-end block, where the claim being tested is that a real
listener answers a real socket with a real snapshot, which a stub cannot
prove. Everywhere else `Hud.bridge` is replaced so the suite stays free.

Four claims are worth stating because each one is a rule that would be easy to
break by accident later:

* **The snapshot is free.** Every panel is SQL and Markdown. A later "just
  summarise the day at the top" has to argue with `test_snapshot_is_free`
  rather than quietly bill a dashboard that redraws all day.
* **EDITH is not reachable here.** She is gated on physical access to the
  terminal, and a loopback port is not that. `test_edith_refused_on_hud`.
* **The mail panel only reads.** `gmail.unseen()` mutates the seen-set that
  `scheduler.py` uses to decide what it has already pushed; a polling panel
  calling it would silently eat V's own notifications.
* **Vault text never becomes markup.** The client builds DOM with
  `textContent`; the test that can be written here is the server-side half —
  that the page ships a CSP and that no handler interpolates data into HTML.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import (commands as commands_mod, hud, index, media, persona,
                    render)
from v.core.engine import Response
from v.tests.test_core import make_vault

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


class Ev:
    """A `shells.ShellEvent`, minus the import.

    `rel` and `rels` are the note paths the real event has carried since the
    stream parser was written; the HUD is the first surface to read them, so
    they are the two fields the viewport tests turn.
    """

    def __init__(self, kind, tool="", detail="", n=0, rel="", rels=None):
        self.kind, self.tool, self.detail, self.n = kind, tool, detail, n
        self.rel, self.rels = rel, list(rels or [])


# --------------------------------------------------------------------------
# arcs


@test
def test_arc_specific_prefix_wins():
    """`vault_memory_write` is a memory call *and* starts with `vault_`. The
    ordered list is why; a dict here read every one of them as `vault` and the
    MEM arc never lit."""
    eq(hud.arc_for("vault_memory_write"), "mem")
    eq(hud.arc_for("vault_voice_learn"), "mem")
    eq(hud.arc_for("vault_search"), "vault")
    eq(hud.arc_for("vault_calendar"), "cal")
    eq(hud.arc_for("gcal_events"), "cal")
    eq(hud.arc_for("gmail_search"), "msg")
    eq(hud.arc_for("vault_send_file"), "msg")


@test
def test_arc_unmapped_is_empty_not_guessed():
    eq(hud.arc_for("mcp__claude-in-chrome__navigate"), "")
    eq(hud.arc_for("Bash"), "")


@test
def test_arc_survives_mcp_prefixing():
    eq(hud.arc_for("mcp__vault__vault_search"), "vault")
    eq(hud.arc_for("mcp__vault__vault_memory_list"), "mem")


# --------------------------------------------------------------------------
# the derived ambient state


@test
def test_state_idle_when_nothing_happened():
    s = hud.Session()
    eq(s.state()[0], "idle")


@test
def test_state_listening_follows_the_keyboard():
    s = hud.Session()
    s.typing = time.time()
    eq(s.state()[0], "listening")


@test
def test_state_thinking_while_a_turn_is_in_flight():
    s = hud.Session()
    s.begin("what's on today")
    eq(s.state()[0], "thinking")


@test
def test_state_thinking_counts_tools_in_flight():
    s = hud.Session()
    s.begin("x")
    s.progress(Ev("tool", "vault_search", "gym"))
    s.progress(Ev("tool", "gcal_events", "today"))
    name, detail = s.state()
    eq(name, "thinking")
    ok("2 tools in flight" in detail, detail)


@test
def test_state_speaking_then_idle():
    """`speaking` is the seconds after an answer, and it has to expire — a
    surface stuck on it is a surface that says V is talking when the room is
    silent."""
    s = hud.Session()
    s.landed(hud.Turn("v", "done", time.time()))
    eq(s.state()[0], "speaking")
    s.spoke_at = time.time() - hud.SPEAK_HOLD - 1
    eq(s.state()[0], "idle")


@test
def test_state_writing_beats_speaking():
    s = hud.Session()
    s.landed(hud.Turn("v", "noted", time.time()), wrote="19 - Assistant/V/x.md",
             confidence=0.9)
    eq(s.state()[0], "writing")


@test
def test_state_error_is_sticky_until_the_next_turn():
    """A fault that clears itself after three seconds is a fault nobody sees.
    It holds until something else is asked."""
    s = hud.Session()
    s.failed("calendar api unreachable")
    eq(s.state()[0], "error")
    eq(s.state()[1], "calendar api unreachable")
    s.begin("try again")
    eq(s.state()[0], "thinking")


@test
def test_typing_does_not_override_a_running_turn():
    s = hud.Session()
    s.begin("x")
    s.typing = time.time()
    eq(s.state()[0], "thinking", "a keystroke must not blank a running turn")


# --------------------------------------------------------------------------
# the tool log


@test
def test_tool_calls_are_timed_when_the_answer_starts():
    s = hud.Session()
    s.progress(Ev("tool", "vault_search", "gym"))
    ok(s.calls[0].ms == 0.0, "in flight, so not yet timed")
    s.progress(Ev("text"))
    ok(s.calls[0].ms > 0, "settled on the first text event")
    eq(s.calls[0].status, "OK")


@test
def test_a_failed_turn_marks_its_calls():
    s = hud.Session()
    s.progress(Ev("tool", "gcal_events", "today"))
    s.failed("boom")
    eq(s.calls[0].status, "ERR")


@test
def test_ticker_is_bounded():
    """The ticker is a marquee in a 28px strip; an unbounded log is a memory
    leak on a page left open for a week."""
    s = hud.Session()
    for i in range(hud.N_TICKER * 3):
        s.progress(Ev("tool", f"vault_read", f"note-{i}"))
    eq(len(s.calls), hud.N_TICKER)


@test
def test_call_label_keeps_the_argument():
    c = hud.Call(at=0, tool="mcp__vault__vault_search", detail="sparse matrix")
    eq(c.label, "vault_search(sparse matrix)")
    eq(hud.Call(at=0, tool="vault_status").label, "vault_status")
    eq(hud.Call(at=0, tool="mcp__claude-in-chrome__navigate").label,
       "chrome · navigate")


@test
def test_progress_never_raises():
    """`shells._emit` swallows what a progress callback raises, so a bug here
    cannot lose an answer — but it must not be relying on that."""
    s = hud.Session()

    class Bad:
        kind = "tool"
        @property
        def tool(self):
            raise RuntimeError("nope")

    s.progress(Bad())          # must not propagate
    eq(s.state()[0], "idle")


@test
def test_arcs_light_from_calls_in_flight():
    s = hud.Session()
    s.begin("x")
    s.progress(Ev("tool", "vault_search", "a"))
    s.progress(Ev("tool", "gmail_search", "b"))
    eq(sorted(s.tools_lit()), ["msg", "vault"])


@test
def test_arcs_hold_briefly_after_the_answer():
    """They go dark the moment the last call returns otherwise — which is
    exactly when you look up at the screen."""
    s = hud.Session()
    s.begin("x")
    s.progress(Ev("tool", "vault_search", "a"))
    s.landed(hud.Turn("v", "ok", time.time()))
    eq(s.tools_lit(), ["vault"])


# --------------------------------------------------------------------------
# session bookkeeping


@test
def test_local_turns_are_the_ones_with_no_model():
    s = hud.Session()
    s.landed(hud.Turn("v", "from the vault", time.time()))
    s.landed(hud.Turn("v", "from a shell", time.time(), model="sonnet", cost=0.02))
    eq(s.live()["session"]["turns"], 2)
    eq(s.live()["session"]["local"], 1)
    eq(s.live()["session"]["spent"], 0.02)


@test
def test_writes_are_capped_and_newest_first():
    s = hud.Session()
    for i in range(9):
        s.landed(hud.Turn("v", "x", time.time()), wrote=f"note-{i}.md",
                 confidence=0.5)
    paths = [w["path"] for w in s.live()["pending"]]
    eq(len(paths), 6)
    eq(paths[0], "note-8.md")


@test
def test_a_write_with_no_confidence_is_certain_not_zero():
    """An unstated confidence means "V did not measure it", and drawing that
    as a 0% bar reads as "V is sure it is wrong"."""
    s = hud.Session()
    s.landed(hud.Turn("v", "x", time.time()), wrote="a.md", confidence=None)
    eq(s.live()["pending"][0]["confidence"], 1.0)


# --------------------------------------------------------------------------
# SSE fan-out


@test
def test_a_stalled_subscriber_is_dropped_not_waited_on():
    """A backgrounded tab stops reading its socket. Blocking a turn on it
    would let a dead browser stall the engine."""
    s = hud.Session()
    q = s.subscribe()
    for _ in range(50):
        s.publish()                    # far past the queue's maxsize
    ok(q.qsize() <= 8, f"queue grew to {q.qsize()}")
    s.unsubscribe(q)
    s.publish()                        # must not raise with no subscribers


@test
def test_publish_carries_a_sequence():
    s = hud.Session()
    q = s.subscribe()
    s.publish()
    s.publish()
    a, b = q.get_nowait(), q.get_nowait()
    ok(b["seq"] > a["seq"], "frames must be ordered")


# --------------------------------------------------------------------------
# the persona gate


@test
def test_edith_refused_on_hud():
    """She is gated on "physically at the terminal", and a port on 127.0.0.1
    is reachable by every process on the machine. If this ever passes, the
    HUD has been quietly given the terminal's trust level."""
    ok(not persona.EDITH.allows(persona.HUD),
       "EDITH must not be reachable from the HUD")
    ok(persona.EDITH.allows(persona.TUI), "…but still from the terminal")


@test
def test_v_and_friday_are_reachable_on_hud():
    ok(persona.V.allows(persona.HUD))
    ok(persona.FRIDAY.allows(persona.HUD))


@test
def test_hud_is_a_known_surface():
    ok(persona.HUD in persona.SURFACES)
    ok(persona.HUD not in persona.PERSISTS_FROM,
       "a summons here must not rewrite the config for every other surface")


# --------------------------------------------------------------------------
# text handling


@test
def test_plain_strips_wikilinks_to_their_label():
    eq(hud._plain("see [[12 - Courses/DS/hw4|HW4]] now"), "see HW4 now")
    eq(hud._plain("see [[12 - Courses/DS/hw4]]"), "see hw4")


@test
def test_plain_keeps_persian_intact():
    """The vault is half Persian and full of ZWNJ. A "tidy up the text" step
    that normalises it is a step that silently rewrites the notes' own words
    on screen."""
    src = "ریسک‌های معروف رو مرور کنم"
    eq(hud._plain(src), src)


@test
def test_plain_truncates_with_an_ellipsis():
    out = hud._plain("x" * 200, 20)
    eq(len(out), 20)
    ok(out.endswith("…"))


@test
def test_sender_is_the_display_name():
    eq(hud._sender('"Quera" <no-reply@quera.org>'), "Quera")
    eq(hud._sender("<sara@example.com>"), "sara@example.com")


# --------------------------------------------------------------------------
# index drift


@test
def test_drift_lists_what_a_rebuild_would_touch():
    root, s = make_vault()
    try:
        index.rebuild(s)
        eq(index.drift(s), [], "a fresh index has drifted nothing")
        p = root / "02 - Work/Issues/ISSUE-1 test issue.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nmore.\n", encoding="utf-8")
        moved = index.drift(s)
        eq(moved, ["02 - Work/Issues/ISSUE-1 test issue.md"])
        ok(index.is_stale(s), "drift and is_stale must agree")
    finally:
        shutil.rmtree(root)


@test
def test_drift_counts_a_deletion():
    root, s = make_vault()
    try:
        index.rebuild(s)
        (root / "02 - Work/Issues/ISSUE-2 broken.md").unlink()
        ok("02 - Work/Issues/ISSUE-2 broken.md" in index.drift(s),
           "a deleted note still sits in the index")
    finally:
        shutil.rmtree(root)


# --------------------------------------------------------------------------
# the wire


class StubBridge:
    """Stands in for the engine thread. Records what it was asked for."""

    def __init__(self, value=None, error=None):
        self.value = value if value is not None else {"stub": True}
        self.error = error
        self.calls = 0

    def call(self, fn, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value

    def stop(self):
        pass


def stand_up(app, port=0):
    srv = hud._make_server(app, port or 0)
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return srv, srv.server_address[1]


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    head = {"Sec-Fetch-Site": "same-origin"}
    if body is not None:
        head["Content-Type"] = "application/json"
    head.update(headers or {})
    conn.request(method, path,
                 json.dumps(body).encode() if body is not None else None, head)
    r = conn.getresponse()
    return r.status, r.read(), dict(r.getheaders())


class BareHud(hud.Hud):
    """A `Hud` with no engine thread behind it."""

    def __init__(self):
        self.s = None
        # No scout: it is a thread and a filesystem walk, and the tests that
        # want one build it explicitly (`ScoutHud`). `Session.watch` stays
        # None, which is exactly the case `_look` is written to survive.
        self.scout = None
        self.session = hud.Session()
        self.bridge = StubBridge()
        self.caches = {}
        self._snap = None
        self._snap_at = 0.0
        self._snap_lock = threading.Lock()


@test
def test_page_and_assets_are_served():
    app = BareHud()
    srv, port = stand_up(app)
    try:
        for path, needle in (("/", b"<title>V \xc2\xb7 HUD</title>"),
                             ("/hud.css", b"--cyan"),
                             ("/hud.js", b"STATES")):
            status, body, _ = request(port, "GET", path)
            eq(status, 200, path)
            ok(needle in body, f"{path} looks wrong")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_every_response_carries_a_csp():
    """The page renders vault text. A CSP that pins it to `self` is what makes
    "a browser tab" an acceptable place to do that: no CDN, no analytics, and
    no outbound path for a note that reaches the DOM."""
    app = BareHud()
    srv, port = stand_up(app)
    try:
        _, _, headers = request(port, "GET", "/")
        csp = headers.get("Content-Security-Policy", "")
        ok("default-src 'self'" in csp, csp)
        ok("object-src 'none'" in csp, csp)
        ok("frame-ancestors 'none'" in csp, csp)
        ok("connect-src 'self'" in csp, csp)
        eq(headers.get("X-Content-Type-Options"), "nosniff")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_cross_site_post_is_refused():
    """Any page in any tab can POST to a loopback port. It cannot forge
    `Sec-Fetch-Site`, and without this check that POST reaches `handle()` —
    which writes."""
    app = BareHud()
    srv, port = stand_up(app)
    try:
        status, body, _ = request(port, "POST", "/api/chat", {"text": "hi"},
                                  headers={"Sec-Fetch-Site": "cross-site"})
        eq(status, 403)
        ok(b"cross-site" in body)
        eq(app.bridge.calls, 0, "it must be refused before the engine")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_same_site_and_none_are_allowed():
    """`none` is what a browser sends for an address typed into the bar."""
    app = BareHud()
    app.bridge = StubBridge({"turn": {}, "live": {}})
    srv, port = stand_up(app)
    try:
        for site in ("same-origin", "none"):
            status, _, _ = request(port, "POST", "/api/typing", {},
                                   headers={"Sec-Fetch-Site": site})
            eq(status, 200, site)
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_empty_chat_is_refused_without_reaching_the_engine():
    app = BareHud()
    srv, port = stand_up(app)
    try:
        status, _, _ = request(port, "POST", "/api/chat", {"text": "   "})
        eq(status, 400)
        eq(app.bridge.calls, 0)
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_asset_path_traversal_is_refused():
    app = BareHud()
    srv, port = stand_up(app)
    try:
        for bad in ("/../../etc/passwd", "/..%2f..%2fetc%2fpasswd",
                    "/hud.py", "/../hud.py"):
            status, _, _ = request(port, "GET", bad)
            ok(status == 404, f"{bad} returned {status}")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_a_busy_engine_serves_the_last_snapshot_stale():
    """The phone widget's contract, on a screen: a picture with a timestamp
    beats a spinner, and beats six panels blanking one at a time."""
    app = BareHud()
    app.bridge = StubBridge({"vault": {"notes": 5}, "day_events": []})
    first = app.snapshot()
    eq(first["stale"], False)
    app.session.busy = True
    again = app.snapshot()
    eq(again["stale"], True)
    eq(app.bridge.calls, 1, "a busy engine must not be queued behind")
    ok("live" in again)


@test
def test_a_timeout_falls_back_rather_than_failing():
    app = BareHud()
    app.bridge = StubBridge({"vault": {"notes": 5}})
    app.snapshot()
    app.bridge = StubBridge(error=hud.HudError(504, "busy"))
    out = app.snapshot()
    eq(out["stale"], True)
    eq(out["vault"]["notes"], 5)


@test
def test_the_first_snapshot_has_nothing_to_fall_back_to():
    app = BareHud()
    app.bridge = StubBridge(error=hud.HudError(504, "busy"))
    try:
        app.snapshot()
        raise AssertionError("should have raised")
    except hud.HudError as exc:
        eq(exc.status, 504)


@test
def test_unknown_routes_are_404_json():
    app = BareHud()
    srv, port = stand_up(app)
    try:
        status, body, headers = request(port, "GET", "/api/nope")
        eq(status, 404)
        ok(headers["Content-Type"].startswith("application/json"))
        eq(json.loads(body)["error"], "not found")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_free_port_skips_one_in_use():
    import socket as sock_mod
    with sock_mod.socket() as s:
        s.setsockopt(sock_mod.SOL_SOCKET, sock_mod.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        taken = s.getsockname()[1]
        s.listen(1)
        eq(hud.free_port(taken), taken + 1)


# --------------------------------------------------------------------------
# the snapshot, against a real vault and a real engine


@test
def test_snapshot_is_free():
    """Everything on this page is SQL and Markdown. A dashboard that redraws
    every four seconds and bills for it is a dashboard you turn off — the same
    reason `glance()` is asserted free for the phone widget."""
    root, s = make_vault()
    try:
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            session = hud.Session()
            caches = {"mail": {"at": time.time() + 1e6, "val":
                               {"state": "off", "items": []}}}
            snap = hud.snapshot(engine, session, caches)
            eq(session.live()["session"]["spent"], 0.0,
               "the snapshot must not spend anything")
            # Shape, so a panel that silently stops being sent is caught.
            for key in ("vault", "gauges", "day_events", "week", "tasks",
                        "deadlines", "mems", "nudges", "mail", "alarm",
                        "agent", "header", "jalali"):
                ok(key in snap, f"snapshot lost {key}")
            eq(snap["agent"], "v")
            ok(snap["vault"]["notes"] > 0)
            ok(isinstance(snap["gauges"]["drift"]["files"], int))
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def the_week_panel_is_a_grid_the_browser_does_not_have_to_parse():
    """Minute offsets, not times.

    The browser lays every block out by arithmetic on `s` and `e`. Sending
    "10:30" instead would make the grid depend on the browser agreeing about
    which zone that is in — which is how a block lands an hour off on exactly
    the machine you are not testing on.
    """
    root, s = make_vault()
    try:
        week = root / "01 - Daily Notes/Weekly/2026"
        week.mkdir(parents=True, exist_ok=True)
        day = dt.date(2026, 9, 20)                      # a Sunday
        sat = dt.date(2026, 9, 19)
        (week / "2026-W38.md").write_text(
            '---\nweek-start: "2026-09-19"\nweek: "2026-W38"\n---\n\n'
            "## Schedule\n"
            "- 2026-09-20 10:30-12:00 · class · Compiler Design @204\n"
            "- 2026-09-20 14:00-16:00 · work · Divar\n"
            "- 2026-09-21 19:00-20:30 · gym\n",
            encoding="utf-8")
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            now = dt.datetime.combine(day, dt.time(11, 0))
            w = hud._week(engine, day, now)

            eq(w["start"], sat.isoformat(), "the week opens on Saturday")
            eq(len(w["days"]), 7)
            eq(w["rel"], "01 - Daily Notes/Weekly/2026/2026-W38.md")

            sunday = w["days"][1]
            eq(sunday["date"], day.isoformat())
            eq([b["kind"] for b in sunday["blocks"]], ["class", "work"])
            first = sunday["blocks"][0]
            eq((first["s"], first["e"]), (630, 720), "minutes, not strings")
            eq(first["where"], "204")
            ok(first["now"], "11:00 is inside the 10:30 class")
            ok(not first["past"])
            ok(w["days"][2]["blocks"][0]["kind"] == "gym")

            # The canvas is always the whole day, so the grid never changes
            # height under you and 03:00 is somewhere you can scroll to
            # rather than somewhere that does not exist.
            eq((w["lo"], w["hi"]), (0, 24 * 60))
            eq(w["focus"], 570, "opens an hour above the earliest block")
            eq(w["claimed"], 90 + 120 + 90)
            eq([k["kind"] for k in w["load"]], ["work", "class", "gym"],
               "heaviest kind first")

            ok(w["exists"], "the week note is really there")
            eq(w["blocks"], 3, "and the panel says how many blocks it found")
            eq(w["now"]["kind"], "class", "what he is in the middle of")
            eq(w["now"]["until"], "12:00")
            # No free-hour tally anywhere in the grid payload. It was a chip
            # under every day header reading "11h free", and a calendar that
            # counts the hours you have not committed is nagging rather than
            # reporting. `vault_free_time` answers it when it is asked.
            ok("free" not in sunday and "free_mins" not in sunday,
               "the day header must not carry a free-hours tally")
            ok("free_total" not in w and "next_free" not in w)

            # The now-line comes from V's clock, not the browser's. V pins its
            # process to Asia/Tehran; the browser uses the machine's zone, and
            # on this laptop those are four and a half hours apart.
            eq(w["now_min"], 11 * 60, "minutes since midnight, from the server")
            # …and no line at all on a week that is not the one `now` is in.
            far = hud._week(engine, dt.date(2027, 3, 1), now)
            eq(far["now_min"], None, "no line for a week that isn't this one")
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def a_block_inside_a_longer_one_nests_instead_of_splitting_it():
    """The layout rule, and the reason it is not just "lanes".

    A 30-minute standup inside an eight-hour work block is one thing that
    happens *during* another, not two things competing for the column.
    Splitting the column spent half the day's width on a stripe of empty work
    block and — worse — put the big block beside the small one instead of
    under it, so the pointer could never reach the thing you aimed at.

    Depth is what fixes both: the contained block is inset, drawn on top, and
    therefore the one that takes the hover and the click.
    """
    root, s = make_vault()
    try:
        week = root / "01 - Daily Notes/Weekly/2026"
        week.mkdir(parents=True, exist_ok=True)
        (week / "2026-W38.md").write_text(
            '---\nweek-start: "2026-09-19"\n---\n\n## Schedule\n'
            "- 2026-09-19 08:00-16:00 · work · Divar\n"
            "- 2026-09-19 12:00-12:30 · event · standup\n"
            "- 2026-09-19 19:00-20:30 · gym\n",
            encoding="utf-8")
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            w = hud._week(engine, dt.date(2026, 9, 19),
                          dt.datetime(2026, 9, 19, 11, 0))
            by = {b["title"]: b for b in w["days"][0]["blocks"]}

            eq((by["Divar"]["depth"], by["Divar"]["lanes"]), (0, 1),
               "the long block keeps the whole column")
            eq((by["standup"]["depth"], by["standup"]["lanes"]), (1, 1),
               "the contained one nests on top of it, full width at its depth")
            eq((by["gym"]["depth"], by["gym"]["lanes"]), (0, 1),
               "a block beside nothing must not be narrowed")

            # The line is the block's identity, and it is what the editor
            # hands back to move or delete it.
            eq(by["gym"]["line"], "- 2026-09-19 19:00-20:30 · gym")
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def a_genuine_partial_overlap_still_splits_the_width():
    """Nesting is for containment. Two blocks neither of which contains the
    other have no honest drawing except side by side — and they must split
    only against each other, not against the block they both sit inside."""
    root, s = make_vault()
    try:
        week = root / "01 - Daily Notes/Weekly/2026"
        week.mkdir(parents=True, exist_ok=True)
        (week / "2026-W38.md").write_text(
            '---\nweek-start: "2026-09-19"\n---\n\n## Schedule\n'
            "- 2026-09-19 08:00-16:00 · work · Divar\n"
            "- 2026-09-19 13:00-14:00 · study · reading\n"
            "- 2026-09-19 13:30-15:00 · paper · writing\n",
            encoding="utf-8")
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            w = hud._week(engine, dt.date(2026, 9, 19),
                          dt.datetime(2026, 9, 19, 11, 0))
            by = {b["title"]: b for b in w["days"][0]["blocks"]}

            eq(by["Divar"]["lanes"], 1, "the container is not split by its contents")
            for title in ("reading", "writing"):
                eq(by[title]["depth"], 1, f"{title} is inside the work block")
                eq(by[title]["lanes"], 2, f"{title} shares its depth with the other")
            eq(sorted(by[t]["lane"] for t in ("reading", "writing")), [0, 1])
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def an_empty_week_still_has_a_grid_to_draw():
    """`lo > hi` would render zero rows and look like a broken panel."""
    root, s = make_vault()
    try:
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            w = hud._week(engine, dt.date(2026, 9, 20),
                          dt.datetime(2026, 9, 20, 11, 0))
            eq((w["lo"], w["hi"]), (0, 24 * 60), "always the whole day")
            eq(w["focus"], 7 * 60, "an empty week opens on the working morning")
            eq(w["claimed"], 0)
            # The page tells these two apart: a week with no note sends you to
            # `v week --ensure`, a week with an empty section sends you to the
            # note, and a snapshot with no panel at all sends you to restart
            # the server. One message for all three sent you to the vault to
            # look for a bug that was in a process.
            ok(not w["exists"], "no week note in this fixture")
            eq(w["blocks"], 0)
            eq(w["now"], None)
            eq(len(w["days"]), 7)
            eq(w["days"][0]["blocks"], [], "an empty day draws nothing")
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


# --------------------------------------------------------------------------
# the task view — the surface where a task can be got rid of


class DirectBridge:
    """Runs the work function here, against a real engine.

    `_Bridge`'s threading is tested above and is not what these are about:
    every claim below is about what the closure inside `Hud.task` does to a
    vault, and a queue in front of it only makes the failure harder to read.
    """

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    def call(self, fn, timeout=None):
        self.calls += 1
        return fn(self.engine)

    def stop(self):
        pass


TASKY_NOTE = ("01 - Daily Notes/2026/07 - July/2026-07-04.md",
              '---\ndate: "2026-07-04"\n---\n\n## Tasks\n'
              "- [ ] read the Kafka rebalance doc\n"
              "- [ ] chase the design review\n")


def tasky_hud():
    """A real engine over a real vault with two real checkboxes in it."""
    root, s = make_vault()
    rel, body = TASKY_NOTE
    (root / rel).write_text(body, encoding="utf-8")
    s.git_enabled = False
    index.rebuild(s, full=True)
    from v.core.engine import Engine
    engine = Engine(s, surface=persona.HUD)
    app = BareHud()
    app.s = s
    app.bridge = DirectBridge(engine)
    return root, s, engine, app


def weeky_hud():
    """A real engine over a real vault with a real week note in it."""
    root, s = make_vault()
    week = root / "01 - Daily Notes/Weekly/2026"
    week.mkdir(parents=True, exist_ok=True)
    (week / "2026-W38.md").write_text(
        '---\nweek-start: "2026-09-19"\nweek: "2026-W38"\n---\n\n'
        "## Schedule\n"
        "- 2026-09-20 10:30-12:00 · class · Compiler Design @204\n",
        encoding="utf-8")
    s.git_enabled = False
    index.rebuild(s, full=True)
    from v.core.engine import Engine
    engine = Engine(s, surface=persona.HUD)
    app = BareHud()
    app.s = s
    app.bridge = DirectBridge(engine)
    return root, s, engine, app


@test
def the_week_can_be_managed_from_the_hud():
    """A calendar you can only look at is a screenshot.

    The TASKS view has written to the vault since it was built; this is the
    same contract for time. Three verbs, each one line of one note.
    """
    root, s, engine, app = weeky_hud()
    rel = "01 - Daily Notes/Weekly/2026/2026-W38.md"
    try:
        out = app.block("add", {"date": "2026-09-20", "kind": "paper",
                                "from": "14:00", "to": "16:00",
                                "label": "draft intro @home"})
        eq(out["ok"], True)
        text = (root / rel).read_text(encoding="utf-8")
        ok("- 2026-09-20 14:00-16:00 · paper · draft intro @home" in text)

        out = app.block("move", {
            "date": "2026-09-20",
            "line": "- 2026-09-20 14:00-16:00 · paper · draft intro @home",
            "from": "15:00", "to": "17:00", "kind": "study",
            "label": "reread Turpin"})
        eq(out["ok"], True)
        text = (root / rel).read_text(encoding="utf-8")
        # The whole line, not a prefix of it: a label edited to drop its
        # `@location` must drop it, rather than keeping one the form never
        # showed and the note now carries invisibly.
        ok("- 2026-09-20 15:00-17:00 · study · reread Turpin\n" in text,
           f"exact line missing from:\n{text}")
        ok("@home" not in text, "the location went with the label that named it")
        ok("draft intro" not in text, "the old line is gone, not duplicated")

        out = app.block("drop", {
            "date": "2026-09-20",
            "line": "- 2026-09-20 15:00-17:00 · study · reread Turpin"})
        eq(out["ok"], True)
        text = (root / rel).read_text(encoding="utf-8")
        ok("reread Turpin" not in text)
        ok("Compiler Design" in text, "the seeded class is untouched")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def a_stale_hud_tab_cannot_clobber_a_week_that_moved():
    """The failure this surface makes possible and has to refuse.

    The browser names a block by its written line. A tab left open on a week
    somebody has since edited is holding a line that is no longer there, and
    the only safe answer is to say so — not to delete whatever is nearest.
    """
    root, s, engine, app = weeky_hud()
    rel = "01 - Daily Notes/Weekly/2026/2026-W38.md"
    before = (root / rel).read_text(encoding="utf-8")
    try:
        for action, body in (
                ("drop", {"date": "2026-09-20",
                          "line": "- 2026-09-20 09:00-10:00 · class · Astrophysics"}),
                ("move", {"date": "2026-09-20",
                          "line": "- 2026-09-20 09:00-10:00 · class · Astrophysics",
                          "from": "11:00"})):
            try:
                app.block(action, body)
                raise AssertionError(f"{action} on a phantom block must refuse")
            except hud.HudError as exc:
                eq(exc.status, 409)
                ok("no such block" in exc.message)
        eq((root / rel).read_text(encoding="utf-8"), before,
           "a refused action must leave the note byte-identical")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def the_hud_refuses_a_block_it_cannot_make_sense_of():
    """Validation before the write, not a traceback after it."""
    root, s, engine, app = weeky_hud()
    rel = "01 - Daily Notes/Weekly/2026/2026-W38.md"
    before = (root / rel).read_text(encoding="utf-8")
    try:
        bad = [
            ({"date": "2026-09-20", "kind": "yoga", "from": "09:00", "to": "10:00"},
             "kind must be one of"),
            ({"date": "2026-09-20", "kind": "gym", "from": "10:00", "to": "09:00"},
             "end after it starts"),
            ({"date": "nonsense", "kind": "gym", "from": "09:00", "to": "10:00"},
             "YYYY-MM-DD"),
            ({"date": "2026-09-20", "kind": "gym", "from": "9am", "to": "10:00"},
             "HH:MM"),
        ]
        for body, why in bad:
            try:
                app.block("add", body)
                raise AssertionError(f"{body} should have been refused")
            except hud.HudError as exc:
                eq(exc.status, 400)
                ok(why in exc.message, f"{exc.message!r} should mention {why!r}")
        eq((root / rel).read_text(encoding="utf-8"), before)
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_task_view_lists_more_than_the_card_and_is_free():
    """The card is six rows beside the conversation and answers "what next".
    The view is the backlog, and it is the only place a task can be dropped or
    silenced — so it is a separate read, and it stays a free one."""
    root, s, engine, app = tasky_hud()
    try:
        for view in hud.TASK_VIEWS:
            out = app.tasks(view)
            eq(out["view"], view)
            for key in ("rows", "total", "counts"):
                ok(key in out, f"{view} lost {key}")
        eq(app.session.live()["session"]["spent"], 0.0,
           "the task view must not spend anything")
        rows = app.tasks("open")["rows"]
        ok({"read the Kafka rebalance doc", "chase the design review"}
           <= {r["title"] for r in rows}, f"got {[r['title'] for r in rows]}")
        ok(all(r["key"] for r in rows),
           "every row needs the key its ignore is written under")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_unknown_task_view_is_refused():
    root, s, engine, app = tasky_hud()
    try:
        app.tasks("everything")
        raise AssertionError("served a view that does not exist")
    except hud.HudError as exc:
        eq(exc.status, 400)
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_ignoring_from_the_hud_edits_no_note():
    """The whole reason IGNORE is a third button and not `dropped`. A daily
    note from July keeps the line exactly as it was typed; only V's opinion of
    it changes, and the page says so on the button."""
    root, s, engine, app = tasky_hud()
    rel, body = TASKY_NOTE
    try:
        row = next(r for r in app.tasks("open")["rows"] if "Kafka" in r["title"])
        out = app.task("ignored", task_id=row["id"])
        eq(out["ok"], True)
        eq((root / rel).read_text(encoding="utf-8"), body,
           "ignoring wrote to the note —")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_an_ignored_task_leaves_the_card_and_the_counts():
    """"For the rest of the world" is the requirement, and the card is the
    rest of the world's nearest edge — if it survives here it survives in the
    briefing and the phone widget too, because all three read `open()`."""
    root, s, engine, app = tasky_hud()
    try:
        row = next(r for r in app.tasks("open")["rows"] if "Kafka" in r["title"])
        before = hud.snapshot(engine, app.session, {})
        ok(any("Kafka" in t["title"] for t in before["tasks"]),
           "the fixture has to start with it on the card")

        app.task("ignored", task_id=row["id"])
        after = hud.snapshot(engine, app.session, {})
        ok(not any("Kafka" in t["title"] for t in after["tasks"]),
           "still on the card after being ignored")
        eq(after["task_counts"]["ignored"], 1)
        eq(after["task_counts"]["open"], before["task_counts"]["open"] - 1)

        listed = app.tasks("ignored")["rows"]
        eq([r["title"] for r in listed], ["read the Kafka rebalance doc"])
        ok(listed[0]["carried"] >= 1, "the entry should say what it is hiding")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_un_ignoring_works_from_the_key_alone():
    """The ignored list is served from the silence list, not from a task
    query, so its rows have no live id — an entry whose note was deleted is
    the one most worth being able to clear. The key is what addresses it."""
    root, s, engine, app = tasky_hud()
    try:
        row = next(r for r in app.tasks("open")["rows"] if "Kafka" in r["title"])
        app.task("ignored", task_id=row["id"])
        listed = app.tasks("ignored")["rows"]
        eq(listed[0]["id"], 0, "an ignored row carries no id —")

        out = app.task("unignored", key=listed[0]["key"])
        eq(out["unchanged"], False)
        ok(any("Kafka" in r["title"] for r in app.tasks("open")["rows"]),
           "it did not come back")
        eq(app.tasks("ignored")["rows"], [])
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_dropping_writes_the_strike_and_ignoring_does_not():
    """The two ways to get rid of a task, side by side, because the only
    difference that matters to the user is which one touches the note."""
    root, s, engine, app = tasky_hud()
    rel, _ = TASKY_NOTE
    try:
        rows = app.tasks("open")["rows"]
        app.task("dropped",
                 task_id=next(r["id"] for r in rows if "design" in r["title"]))
        text = (root / rel).read_text(encoding="utf-8")
        ok("- [-] chase the design review" in text, text)
        ok("- [ ] read the Kafka rebalance doc" in text,
           "dropping one must leave the other alone")
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_unknown_task_state_is_refused():
    root, s, engine, app = tasky_hud()
    try:
        app.task("deleted", task_id=1)
        raise AssertionError("accepted a verb that does not exist")
    except hud.HudError as exc:
        eq(exc.status, 400)
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_a_task_write_with_no_state_is_refused_not_defaulted():
    """`/api/task` used to read a missing state as `done`, so a body that
    failed to parse — a bad request, a truncated POST — meant "tick a task".
    Of the six verbs it accepts, that is the one that must never be reached by
    accident, and it was the one you got for free."""
    app = BareHud()
    srv, port = stand_up(app)
    try:
        for body in ({}, {"id": 1}, {"id": 1, "state": ""},
                     {"id": 1, "state": None}):
            status, raw, _ = request(port, "POST", "/api/task", body)
            eq(status, 400, f"{body} should have been refused")
            ok("state is required" in json.loads(raw)["error"])
        eq(app.bridge.calls, 0, "nothing should have reached the engine")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_the_nudge_panel_can_silence_and_restore_a_check():
    """`⊗` on the panel. The round trip has to change what the *next*
    snapshot says, or the click reads as having done nothing — the panel is
    drawn from a cache the poll refreshes every four seconds."""
    root, s, engine, app = tasky_hud()
    try:
        fired = hud._nudges(engine, dt.date.today())
        ok(fired, "expected the fixture to raise at least one nudge")
        key = fired[0]["key"]
        ok(key, "every nudge on the panel needs a handle to silence it by")

        app.snapshot()                       # warm the cache the panel reads
        out = app.nudge("silence", key)
        eq(out["ok"], True)
        ok(app._snap is None, "the cached panel must be dropped, not served")

        snap = app.snapshot()
        ok(all(n["key"] != key for n in snap["nudges"]),
           "a silenced check is off the panel")
        eq([x["key"] for x in snap["nudges_off"]], [key],
           "…and on the list underneath it, which is what makes it undoable")

        app.nudge("restore", key)
        snap = app.snapshot()
        ok(any(n["key"] == key for n in snap["nudges"]), "restored whole")
        eq(snap["nudges_off"], [])
    finally:
        engine.close()
        shutil.rmtree(root)


@test
def test_a_nudge_write_with_no_action_is_refused_not_defaulted():
    """Same rule as `/api/task`, for the same reason: a truncated POST must
    not be readable as "hide this"."""
    app = BareHud()
    srv, port = stand_up(app)
    try:
        for body in ({}, {"key": "gym.gap"}, {"key": "gym.gap", "action": ""},
                     {"key": "gym.gap", "action": None}):
            status, raw, _ = request(port, "POST", "/api/nudge", body)
            eq(status, 400, f"{body} should have been refused")
            ok("action is required" in json.loads(raw)["error"])
        # And a verb that does not exist, and a verb with nothing to act on.
        for body, needle in (({"action": "delete", "key": "x"}, "silence"),
                             ({"action": "silence", "key": " "}, "key")):
            status, raw, _ = request(port, "POST", "/api/nudge", body)
            eq(status, 400)
            ok(needle in json.loads(raw)["error"], raw)
        eq(app.bridge.calls, 0, "nothing should have reached the engine")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_the_page_and_the_server_agree_on_the_nudge_vocabulary():
    """The task-vocabulary test's argument, applied to the pair of verbs this
    panel has: a button the server refuses would ship green."""
    js = (Path(hud.ASSETS) / "hud.js").read_text(encoding="utf-8")
    import re
    verbs = set(re.findall(r"nudgeAction\('(\w+)'", js))
    eq(verbs, {"silence", "restore"},
       f"the page sends verbs the server does not accept: {verbs}")


@test
def test_the_page_and_the_server_agree_on_the_task_vocabulary():
    """Two halves of one contract in two languages, which is the shape that
    rots. The page draws a button per verb and a chip per view; the server
    accepts a fixed list of each. A verb invented on either side is a button
    that 400s, and it would ship green."""
    js = (Path(hud.ASSETS) / "hud.js").read_text(encoding="utf-8")
    import re
    buttons = set(re.findall(r"^\s{2}(\w+):\s+\{ glyph:", js, re.M))
    ok(buttons, "could not find TASK_BUTTONS in the page")
    ok(buttons <= set(hud.TASK_ACTIONS),
       f"the page draws verbs the server refuses: "
       f"{buttons - set(hud.TASK_ACTIONS)}")

    # The half that actually broke, and the reason this test is worth its
    # regexes. A subset check on `TASK_BUTTONS` says nothing about the other
    # direction: a row asked for `open` on a ticked task, no button had that
    # name, and `undefined.label` took the whole pane down — silently, because
    # the render memo had already been advanced past the frame that threw.
    # Every verb a row can name must have a button and be a verb the server
    # accepts.
    used = set()
    for expr in re.findall(r"const verbs = (.+?);", js, re.S):
        used |= set(re.findall(r"'(\w+)'", expr))
    ok(used, "could not find the row's verb list")
    ok(used <= buttons, f"rows name buttons that do not exist: {used - buttons}")
    ok(used <= set(hud.TASK_ACTIONS),
       f"rows name verbs the server refuses: {used - set(hud.TASK_ACTIONS)}")

    views = set(re.findall(r"\{ id: '(\w+)',\s+label: '[A-Z]+',\s+empty:", js))
    eq(views, set(hud.TASK_VIEWS),
       "the filter chips and the server's views have drifted")


@test
def test_snapshot_survives_a_broken_panel():
    """One check raising must not take the page with it — `NudgeEngine.all`
    has the same rule for the same reason."""
    root, s = make_vault()
    try:
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            class Boom:
                def all(self, **kw):
                    raise RuntimeError("check exploded")
            engine.nudges = Boom()
            snap = hud.snapshot(engine, hud.Session(), {})
            eq(snap["nudges"], [])
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def test_mail_panel_never_calls_unseen():
    """`gmail.unseen()` marks what it returns as seen, and that set is how
    `scheduler.py` decides what it has already pushed. A panel polling it
    every five minutes would eat V's own notifications before he read them."""
    import v.core.gmail as gmail_mod

    calls = []

    class FakeGmail:
        def __init__(self, s):
            pass
        def configured(self):
            return True
        def authorised(self):
            return True
        def account(self):
            return "x@example.com"
        def search(self, q, limit=10):
            calls.append(("search", q))
            return []
        def unseen(self, q, *, limit=10):
            calls.append(("unseen", q))
            raise AssertionError("the HUD must never mark mail seen")

    real = gmail_mod.Gmail
    gmail_mod.Gmail = FakeGmail
    try:
        class FakeEngine:
            s = None
        out = hud._mail(FakeEngine(), {})
        eq(out["state"], "on")
        eq([c[0] for c in calls], ["search"])
    finally:
        gmail_mod.Gmail = real


@test
def test_mail_panel_reports_not_connected_rather_than_hiding_it():
    """A blank inbox and a disconnected one look identical, and only one is
    worth doing something about."""
    import v.core.gmail as gmail_mod

    class Unconfigured:
        def __init__(self, s):
            pass
        def configured(self):
            return False

    real = gmail_mod.Gmail
    gmail_mod.Gmail = Unconfigured
    try:
        class FakeEngine:
            s = None
        eq(hud._mail(FakeEngine(), {})["state"], "unconfigured")
    finally:
        gmail_mod.Gmail = real


@test
def test_mail_is_cached():
    hits = []

    class Counting:
        def __init__(self, s):
            hits.append(1)
        def configured(self):
            return False

    import v.core.gmail as gmail_mod
    real = gmail_mod.Gmail
    gmail_mod.Gmail = Counting
    try:
        class FakeEngine:
            s = None
        cache = {}
        e = FakeEngine()
        hud._mail(e, cache)
        hud._mail(e, cache)
        hud._mail(e, cache)
        eq(len(hits), 1, "the one non-free panel must not poll Google")
    finally:
        gmail_mod.Gmail = real


@test
def test_alarm_fires_on_a_real_deadline_only():
    """The mockup's `alarmMode` is a prop you flip; here it is a fact, and it
    has to go quiet by itself when nothing is due."""
    root, s = make_vault()
    try:
        due = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        (root / "03 - University/Assignments").mkdir(parents=True, exist_ok=True)
        (root / "03 - University/Assignments/HW4.md").write_text(
            f'---\ntype: assignment\ntitle: HW4\ncourse: DS\n'
            f'due-date: "{due}"\nstatus: todo\n---\n\nBody.\n', encoding="utf-8")
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            caches = {"mail": {"at": time.time() + 1e6,
                               "val": {"state": "off", "items": []}}}
            snap = hud.snapshot(engine, hud.Session(), caches)
            eq(snap["alarm"]["on"], True)
            ok("HW4" in snap["alarm"]["label"] or "DS" in snap["alarm"]["label"],
               snap["alarm"]["label"])
        finally:
            engine.close()

        # …and quiet again once it is three weeks out. A second engine rather
        # than surgery on the first: `q`, `memory` and `reminders` share one
        # sqlite connection, so closing the query facade closes it under the
        # other two.
        far = (dt.date.today() + dt.timedelta(days=20)).isoformat()
        (root / "03 - University/Assignments/HW4.md").write_text(
            f'---\ntype: assignment\ntitle: HW4\ncourse: DS\n'
            f'due-date: "{far}"\nstatus: todo\n---\n\nBody.\n',
            encoding="utf-8")
        index.rebuild(s)
        engine = Engine(s, surface=persona.HUD)
        try:
            snap = hud.snapshot(engine, hud.Session(), caches)
            eq(snap["alarm"]["on"], False)
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def test_alarm_label_keeps_persian_whole():
    """`.upper()` is a no-op on Persian and an 18-character cut lands
    mid-word; the chip ellipsises in CSS instead."""
    root, s = make_vault()
    try:
        due = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        (root / "03 - University/Assignments").mkdir(parents=True, exist_ok=True)
        # A ZWNJ (نیم‌فاصله) in the title, because the title is what the chip
        # shows — a course name is a sentence in this vault and does not fit.
        title = "تمرین‌های سری دهم"
        course = "آزمایشگاه طراحی سیستم‌های دیجیتال"
        (root / "03 - University/Assignments/lab.md").write_text(
            f'---\ntype: assignment\ntitle: {title}\ncourse: {course}\n'
            f'due-date: "{due}"\nstatus: todo\n---\n\nBody.\n', encoding="utf-8")
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            caches = {"mail": {"at": time.time() + 1e6,
                               "val": {"state": "off", "items": []}}}
            snap = hud.snapshot(engine, hud.Session(), caches)
            label = snap["alarm"]["label"]
            ok("…" not in label, label)
            ok("‌" in label, f"ZWNJ must survive: {label!r}")
            ok(title in label, f"the title must be whole: {label!r}")
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


# --------------------------------------------------------------------------
# slash commands
#
# The bug these are written against: the HUD had no notion of commands, so
# `/new` went to the router as a sentence, Sonnet answered "Ready, sir.", it
# cost $0.10 and the session it was meant to end carried on underneath.


@test
def test_the_command_table_is_shared_with_the_terminal():
    """One table, or `/new` works in one prompt and is a paid model turn in
    the other — which is exactly what happened."""
    from v import cli
    ok(cli.COMMANDS is commands_mod.COMMANDS,
       "cli must re-export the shared table, not keep its own")
    eq(cli.REPL_HELP, commands_mod.REPL_HELP)


@test
def test_every_command_has_a_spoken_equivalent():
    """The brief's rule: slash is a hint, not a language. A command nobody can
    say is a feature that only exists for people who read the source."""
    silent = [c for c, _, said in commands_mod.COMMANDS
              if not said and c not in ("/copy", "/clear", "/help")]
    eq(silent, [], "these need a spoken form")


@test
def test_split_separates_command_from_argument():
    eq(commands_mod.split("/undo abc"), ("undo", "abc"))
    eq(commands_mod.split("/new"), ("new", ""))
    eq(commands_mod.split("/"), ("", ""))
    eq(commands_mod.split("hello"), None)
    eq(commands_mod.split("what is 1/2"), None)


@test
def test_suggest_is_prefix_on_the_name_only():
    """Matching the description too makes the list jump around under you as
    you type a word that happens to appear in one."""
    eq([c[0] for c in commands_mod.suggest("/n")], ["/nudges", "/new"])
    eq(commands_mod.suggest("/zzz"), [])
    eq(commands_mod.suggest("brief"), [], "not a command line at all")
    ok(any(c[0] == "/why" for c in commands_mod.suggest("/")))


@test
def test_prefs_round_trip():
    root, s = make_vault()
    try:
        eq(commands_mod.prefs(s.state_dir), {})
        commands_mod.save_prefs(s.state_dir, local_only=True)
        eq(commands_mod.prefs(s.state_dir)["local_only"], True)
        commands_mod.save_prefs(s.state_dir, quiet_until="2026-09-07T08:00:00")
        got = commands_mod.prefs(s.state_dir)
        eq(got["local_only"], True, "save must merge, not replace")
        eq(got["quiet_until"], "2026-09-07T08:00:00")
    finally:
        shutil.rmtree(root)


class NewSessionBridge:
    """Records that the engine's session was actually cycled."""

    def __init__(self):
        self.ended = []
        self.started = 0

    def call(self, fn, timeout=None):
        outer = self

        class FakeEngine:
            def end_session(self, reason="quit"):
                outer.ended.append(reason)
            def start_session(self):
                outer.started += 1
                return []
        return fn(FakeEngine())

    def stop(self):
        pass


@test
def test_new_ends_the_session_without_reaching_a_model():
    app = BareHud()
    app.bridge = NewSessionBridge()
    app.session.turns.append(hud.Turn("operator", "brief me", time.time()))
    app.session.turns.append(hud.Turn("v", "…", time.time(), model="sonnet-5",
                                      cost=0.11))
    out = app.command("/new")
    eq(app.bridge.ended, ["new"], "the conversation must be filed, not dropped")
    eq(app.bridge.started, 1, "…and a fresh one opened")
    eq(out["turn"]["cost"], 0.0, "/new is not a model turn")
    eq(out["turn"]["model"], "")
    # The transcript belongs to the conversation that just ended; only the
    # confirmation survives.
    eq(len(out["live"]["turns"]), 1)
    eq(out["live"]["turns"][0]["role"], "v")


@test
def test_new_resets_every_counter_on_the_session():
    """`/new` opens a new conversation, so the block labelled SESSION has to
    describe the new one — id, uptime and turn counts included.

    This asserted the opposite until the budget gauge learned to read
    `spend_today()`. The old argument — that a gauge zeroing on every change
    of subject tells you the day was free — was right about the gauge and
    wrong about where to fix it: the money is a property of the day, not of
    this object, and `test_the_budget_gauge_is_the_days_not_the_sessions`
    is where that claim now lives.
    """
    app = BareHud()
    app.bridge = NewSessionBridge()
    app.session.landed(hud.Turn("v", "x", time.time(), model="sonnet-5",
                                cost=0.42))
    before = app.session.live()["session"]
    eq(before["spent"], 0.42)
    time.sleep(0.01)                 # so a kept `started` would be visible
    app.command("/new")
    after = app.session.live()["session"]
    eq(after["spent"], 0.0, "the conversation's spend went with it")
    eq(after["turns"], 1, "only the confirmation, which is itself a turn")
    eq(after["local"], 1, "…and it was free")
    eq(after["latency_ms"], 0)
    eq(after["model"], "")
    ok(after["id"] != before["id"] or after["up"] <= before["up"],
       "the footer's session line must describe the new conversation")


@test
def test_the_budget_gauge_is_the_days_not_the_sessions():
    """The bar fills against `daily_budget_usd`, so what fills it has to be a
    daily number — read off `hermes.jsonl`, which every surface appends to,
    which is why a run made over Telegram is on it and why `/new` cannot zero
    it.

    And `list_cost_usd`, never `Response.cost`: on a subscription `cost` is
    0.0 by design, so a gauge summing it would sit at $0.00 through a day that
    used half the plan. That was the first cut of this function.
    """
    root, s = make_vault()
    try:
        today = dt.date.today().isoformat()
        s.log_dir.mkdir(parents=True, exist_ok=True)
        with (s.log_dir / "hermes.jsonl").open("w") as fh:
            for at, listed in ((f"{today}T09:00:00", 0.10),
                               (f"{today}T11:30:00", 0.0),
                               (f"{today}T18:05:00", 0.32),
                               ("2026-01-01T23:59:00", 9.99)):   # another day
                fh.write(json.dumps({"at": at, "list_cost_usd": listed,
                                     "cost": 0.0}) + "\n")
            fh.write("{ truncated\n")      # a half-written line, mid-flush
        day = hud.spend_today(s)
        eq(round(day["used"], 4), 0.42, "yesterday must not be on today's bar")
        eq(day["turns"], 3, "the unparseable line is skipped, not counted")
        # A day with no paid runs is $0.00 and not an error.
        eq(hud.spend_today(s, dt.date(2000, 1, 1))["used"], 0.0)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_clear_empties_the_screen_but_not_the_conversation():
    app = BareHud()
    app.bridge = NewSessionBridge()
    app.session.turns.append(hud.Turn("v", "x", time.time()))
    out = app.command("/clear")
    eq(out["turn"], None, "clearing the screen is not something V says")
    eq(out["live"]["turns"], [])
    eq(app.bridge.ended, [], "the session must not be filed by /clear")


@test
def test_unknown_command_is_refused_for_free():
    """The whole point: an unrecognised slash must not become a paid guess."""
    app = BareHud()
    app.bridge = StubBridge()
    out = app.command("/bogus")
    eq(app.bridge.calls, 0, "it must not reach the engine at all")
    eq(out["turn"]["cost"], 0.0)
    eq(out["turn"]["state"], "refused")
    ok("/bogus" in out["turn"]["text"])


@test
def test_client_side_commands_are_refused_at_the_server():
    """`/why`, `/help` and `/copy` are the page's. Reaching the server means
    the client sent something it should have kept."""
    app = BareHud()
    app.bridge = StubBridge()
    for cmd in ("/why", "/help", "/copy"):
        try:
            app.command(cmd)
            raise AssertionError(f"{cmd} should have been refused")
        except hud.HudError as exc:
            eq(exc.status, 400)
    eq(app.bridge.calls, 0)


@test
def test_command_endpoint_rejects_a_plain_sentence():
    app = BareHud()
    srv, port = stand_up(app)
    try:
        status, _, _ = request(port, "POST", "/api/command",
                               {"text": "what is on today"})
        eq(status, 400)
        eq(app.bridge.calls, 0)
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_commands_endpoint_hides_what_the_hud_cannot_do():
    """`/quit` is a terminal idea; a browser tab has a close button. Listing
    it would be offering something that does nothing."""
    app = BareHud()
    srv, port = stand_up(app)
    try:
        status, body, _ = request(port, "GET", "/api/commands")
        eq(status, 200)
        names = [c["cmd"] for c in json.loads(body)["commands"]]
        ok("/new" in names)
        ok("/quit" not in names)
        eq(len(names), len(commands_mod.COMMANDS) - len(commands_mod.NOT_ON_HUD))
        for row in json.loads(body)["commands"]:
            ok(set(row) == {"cmd", "what", "said"}, row)
    finally:
        srv.shutdown(); srv.server_close()


# --------------------------------------------------------------------------
# the viewport
#
# Four claims worth stating, for the same reason the ones at the top of this
# file are:
#
# * **Nothing here runs on the thread answering.** A picture is found by
#   `_Scout`; if finding one ever moved onto `_progress`'s thread, a note read
#   — or worse, a Neshan fetch — would be latency added to an answer in order
#   to decorate it. `test_scouting_never_runs_on_the_callers_thread`.
# * **`/api/media` has no filename in it.** The id is a hash of a path the
#   session already decided to show, so traversal is not defended against — it
#   is unsayable. `test_media_is_addressed_by_handle_not_by_path`.
# * **A frame is a view, not a delivery.** Clearing the viewport must not
#   touch the file. That is also why `media.Spool` exists beside the outbox in
#   `files.py` rather than reusing it: that one is claimed once, and a HUD
#   draining it would eat the chart the phone was waiting for.
# * **A picture may never cost an answer.** Every failure mode here — an
#   unreadable note, a deleted file, no Neshan key — is a frame that does not
#   appear. `test_a_broken_picture_is_never_a_fault`.


class ScoutHud(hud.Hud):
    """A `Hud` with a real scout over a real (throwaway) vault, and no engine
    behind it. The scout is the half these tests are about."""

    def __init__(self, s):
        self.s = s
        self.session = hud.Session()
        self.scout = hud._Scout(s, self.session)
        self.session.watch = self.scout.saw
        self.bridge = StubBridge()
        self.caches = {}
        self._snap = None
        self._snap_at = 0.0
        self._snap_lock = threading.Lock()


def png(path: Path, size: int = 64) -> Path:
    """A file that is a PNG as far as everything here is concerned."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * size)
    return path


def embedding_note(root: Path, rel: str, *, shot: str = "repro.png") -> str:
    png(root / "attachments" / shot)
    (root / rel).write_text(
        "---\ntype: issue\n---\n\nSee ![[" + shot + "]] for the repro.\n",
        encoding="utf-8")
    return rel


def wait_for_frames(session, n: int = 1, timeout: float = 5.0):
    end = time.time() + timeout
    while time.time() < end:
        if len(session.frames) >= n:
            break
        time.sleep(0.02)
    return session.frames


@test
def test_embeds_resolve_by_bare_filename():
    """Obsidian resolves `![[Pasted image ….png]]` by basename, anywhere in
    the vault. Every embed in the real vault is written that way, so a
    resolver that only tried the literal string as a path would find nothing
    at all."""
    root, s = make_vault()
    try:
        rel = embedding_note(root, "02 - Work/Issues/ISSUE-1 test issue.md")
        found = media.images_in_note(s, rel)
        eq(len(found), 1)
        eq(found[0].path.name, "repro.png")
        eq(found[0].source, "note")
        eq(found[0].rel, rel)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_both_embed_spellings_in_document_order():
    """A note that mixes Obsidian's `![[x]]` with an imported `![alt](x)` must
    show its pictures in the order it embeds them, not one spelling first."""
    root, s = make_vault()
    try:
        for name in ("a.png", "b.png", "c.png"):
            png(root / "attachments" / name)
        (root / "note.md").write_text(
            "![alt one](attachments/a.png)\n\n![[b.png]]\n\n"
            "![alt three](attachments/c.png)\n", encoding="utf-8")
        found = media.images_in_note(s, "note.md")
        eq([f.path.name for f in found], ["a.png", "b.png", "c.png"])
        eq(found[0].title, "alt one", "the alt text is the picture's name")
        eq(found[1].title, "b", "with no alt, the filename is")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_embeds_are_capped_per_note():
    """A bug-report note carries eight screenshots of the same screen. The
    first few are the evidence; the rest are the scroll."""
    root, s = make_vault()
    try:
        body = []
        for i in range(9):
            png(root / "attachments" / f"s{i}.png")
            body.append(f"![[s{i}.png]]")
        (root / "note.md").write_text("\n\n".join(body), encoding="utf-8")
        eq(len(media.images_in_note(s, "note.md")), media.N_PER_NOTE)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_an_embed_cannot_point_out_of_the_vault():
    """A note is prose, and `../../` in one is not a path this surface
    follows."""
    root, s = make_vault()
    outside = png(root.parent / f"outside-{root.name}.png")
    try:
        eq(media.resolve_embed(s, "../" + outside.name), None)
        eq(media.resolve_embed(s, "/etc/hosts"), None)
        eq(media.resolve_embed(s, "https://example.com/x.png"), None)
    finally:
        outside.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


@test
def test_only_pictures_are_frames():
    """A PDF and a video are things to open, not things to glance at — a
    viewport that pops one open mid-answer is one nobody leaves on."""
    eq(media.kind_for("a.png"), "image")
    eq(media.kind_for("a.html"), "page")
    for name in ("a.pdf", "a.mp4", "a.md", "a.zip", "a"):
        eq(media.kind_for(name), "", name)


@test
def test_a_citation_becomes_a_note_path():
    """The structured path — "where is Cafe A?", answered from SQL — runs no
    shell and emits no `ShellEvent`, so the citation is the only trace it
    leaves of what it read."""
    eq(media.rel_from_citation("[[09 - A/05 - Locations/Cafe A|Cafe A]]"),
       "09 - A/05 - Locations/Cafe A.md")
    eq(media.rel_from_citation("[[15 - DUNG/18 - Mascot]]"),
       "15 - DUNG/18 - Mascot.md")
    for junk in ("", "not a link", "[[https://example.com]]", "[[]]"):
        eq(media.rel_from_citation(junk), "", junk)


@test
def test_the_spool_opens_at_the_end_not_the_start():
    """Frames are a live view of what V is doing, not a history of every chart
    it has ever drawn. A HUD started at noon must not open onto this morning's
    renders."""
    root, s = make_vault()
    try:
        media.announce(png(s.state_dir / "renders/old.png"), settings=s)
        spool = media.Spool(s)             # opened *after* the old one
        eq(spool.take(), [])
        media.announce(png(s.state_dir / "renders/new.png"),
                       title="new one", caption="from the test", settings=s)
        got = spool.take()
        eq([g.path.name for g in got], ["new.png"])
        eq(got[0].title, "new one")
        eq(got[0].source, "render")
        eq(spool.take(), [], "reading twice must not replay it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_an_announced_file_that_is_gone_is_not_a_frame():
    root, s = make_vault()
    try:
        spool = media.Spool(s)
        doomed = png(s.state_dir / "renders/doomed.png")
        media.announce(doomed, settings=s)
        doomed.unlink()
        eq(spool.take(), [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_saving_a_render_announces_it():
    """`render.save` is the choke point every artifact passes through, which
    is the whole reason the announcement lives there: a render added next year
    reaches the viewport without anyone wiring it."""
    root, s = make_vault()
    try:
        spool = media.Spool(s)
        page = render.save("<p>hi</p>", slug="a chart", title="A chart",
                           caption="4 points", settings=s)
        blob = render.save_bytes(b"\x89PNG\r\n\x1a\n", slug="a route",
                                 title="A to B", settings=s)
        got = {g.title: g for g in spool.take()}
        eq(sorted(got), ["A chart", "A to B"])
        eq(got["A chart"].kind, "page")
        eq(got["A chart"].caption, "4 points")
        eq(got["A chart"].path, page)
        eq(got["A to B"].kind, "image")
        eq(got["A to B"].path, blob)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_tool_event_puts_the_notes_pictures_on_screen():
    """The ambient half, and the one the design is for: the note paths arrive
    while the model is still writing, so the picture is on screen before the
    sentence about it is."""
    root, s = make_vault()
    app = None
    try:
        rel = embedding_note(root, "02 - Work/Issues/ISSUE-1 test issue.md")
        app = ScoutHud(s)
        app.session.progress(Ev("tool", tool="vault_read", rel=rel))
        frames = wait_for_frames(app.session)
        eq(len(frames), 1)
        eq(frames[0].source, "note")
        eq(frames[0].rel, rel)
        eq(frames[0].kind, "image")
        ok(frames[0].id in app.session.media)
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_search_result_lights_every_note_it_named():
    """`rel` says what a call was about; `rels` says what it came back
    holding, which for `vault_search` is eight notes at once. Most of the
    screenshots come through the second one."""
    root, s = make_vault()
    app = None
    try:
        a = embedding_note(root, "02 - Work/Issues/ISSUE-1 test issue.md",
                           shot="one.png")
        b = embedding_note(root, "02 - Work/Issues/ISSUE-2 broken.md",
                           shot="two.png")
        app = ScoutHud(s)
        app.session.progress(Ev("tool_result", tool="vault_search",
                                rels=[a, b]))
        frames = wait_for_frames(app.session, 2)
        eq(sorted(f.title for f in frames), ["one", "two"])
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_directory_listing_is_not_nine_maps():
    """2026-09-08, and the reason `chosen` and `listed` are separate.

    Asked to draw a path between three places, the model ran `Glob` over the
    Locations folder to find them. `shells._result_rels` scrapes every
    `NN - ….md` out of a result, so a *directory listing* arrived looking
    exactly like twelve notes the answer was about — and the viewport filled
    with maps of the alphabetically-first cafés in the vault, at one Neshan
    request each.

    A listed path may not become a map. Only a note the model named in an
    argument, or one the answer cited, may.
    """
    root, s = make_vault()
    app = None
    try:
        loc = "09 - Anti-Library/05 - Locations"
        for name in ("Andora", "Agor", "Afghani"):
            (root / loc / f"{name}.md").write_text(
                f'---\ntype: place\nname: {name}\nlocation: "1,2"\n---\n\nx\n',
                encoding="utf-8")
        listing = [f"{loc}/{n}.md" for n in ("Andora", "Agor", "Afghani",
                                             "Cafe A")]
        app = ScoutHud(s)
        calls = []
        import v.core.media as media_mod
        real = media_mod.place_map
        media_mod.place_map = lambda st, rel: calls.append(rel)
        try:
            app.session.progress(Ev("tool_result", tool="Glob", rels=listing))
            time.sleep(0.4)
            eq(calls, [], "a directory listing cost a Neshan request")
            eq(app.session.frames, [])
            # The same note, *chosen* by a call, still gets its map.
            app.session.progress(Ev("tool", tool="vault_read",
                                    rel=f"{loc}/Cafe A.md"))
            time.sleep(0.4)
            eq(calls, [f"{loc}/Cafe A.md"])
        finally:
            media_mod.place_map = real
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_listed_note_still_gives_up_its_screenshots():
    """The other half: a search that surfaces a bug report with a screenshot
    in it is exactly the ambient case this was built for. Images are a local
    file read and cost nothing, so a listed note may still contribute them —
    three notes' worth, past which a result set has stopped being an answer
    and gone back to being a list."""
    root, s = make_vault()
    app = None
    try:
        rels = []
        for i in range(6):
            rel = f"bug{i}.md"
            png(root / "attachments" / f"shot{i}.png")
            (root / rel).write_text(f"![[shot{i}.png]]\n", encoding="utf-8")
            rels.append(rel)
        app = ScoutHud(s)
        app.session.progress(Ev("tool_result", tool="vault_search", rels=rels))
        time.sleep(0.6)
        eq(len(app.session.frames), hud.N_LOOSE)
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_maps_are_capped_per_turn():
    """Even a chosen note is a Neshan request. No question is about four
    places at once, and a quota is not a thing to spend on a decoration."""
    root, s = make_vault()
    app = None
    try:
        loc = "09 - Anti-Library/05 - Locations"
        names = [f"P{i}" for i in range(6)]
        for name in names:
            (root / loc / f"{name}.md").write_text(
                f'---\ntype: place\nname: {name}\nlocation: "1,2"\n---\n\nx\n',
                encoding="utf-8")
        app = ScoutHud(s)
        calls = []
        import v.core.media as media_mod
        real = media_mod.place_map
        media_mod.place_map = lambda st, rel: calls.append(rel)
        try:
            for name in names:
                app.session.progress(Ev("tool", tool="vault_read",
                                        rel=f"{loc}/{name}.md"))
            time.sleep(0.6)
            eq(len(calls), hud.N_MAPS)
        finally:
            media_mod.place_map = real
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_the_same_picture_twice_in_one_turn_is_one_frame():
    """`vault_search` names the note, then `vault_read` reads it. A filmstrip
    with the same frame in it twice reads as a bug in the surface rather than
    as two mentions of one picture."""
    root, s = make_vault()
    app = None
    try:
        rel = embedding_note(root, "02 - Work/Issues/ISSUE-1 test issue.md")
        app = ScoutHud(s)
        app.session.progress(Ev("tool", tool="vault_search", rels=[rel]))
        wait_for_frames(app.session)
        app.session.progress(Ev("tool_result", tool="vault_read", rel=rel))
        time.sleep(0.3)
        eq(len(app.session.frames), 1)
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_new_turn_lets_the_same_note_show_again():
    """The frames stay across a question — a follow-up is usually *about* what
    is on screen — but the scout forgets which notes it has been through, so
    asking again about the same note shows it again."""
    root, s = make_vault()
    app = None
    try:
        rel = embedding_note(root, "02 - Work/Issues/ISSUE-1 test issue.md")
        app = ScoutHud(s)
        app.session.progress(Ev("tool", tool="vault_read", rel=rel))
        wait_for_frames(app.session)
        app.session.clear_frames()
        app.session.begin("and again?")
        time.sleep(0.2)
        app.session.progress(Ev("tool", tool="vault_read", rel=rel))
        eq(len(wait_for_frames(app.session)), 1)
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_scouting_never_runs_on_the_callers_thread():
    """The rule `_Scout` exists for. A note read — never mind a Neshan fetch —
    on the thread running the turn is latency added to the answer in order to
    decorate it. And the hand-off must not block even when the scout is far
    behind: a dropped picture is not worth a millisecond of an answer."""
    root, s = make_vault()
    app = None
    try:
        app = ScoutHud(s)
        seen = []
        app.session.watch = (lambda chosen, listed:
                             seen.append(threading.current_thread()))
        app.session.progress(Ev("tool", tool="vault_read", rel="x.md"))
        eq(seen, [threading.current_thread()],
           "the hand-off is synchronous; the work is not")
        app.session.watch = app.scout.saw
        app.scout.stop_flag.set()                  # nothing is draining now
        start = time.time()
        for _ in range(400):
            app.session.progress(Ev("tool", tool="t", rel="y.md"))
        ok(time.time() - start < 5, "a full queue blocked the turn")
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_broken_picture_is_never_a_fault():
    """Every failure here is a frame that does not appear. The viewport is
    ambient — a picture nobody asked for out loud must not be able to put a
    fault in the header, still less lose an answer."""
    root, s = make_vault()
    app = None
    try:
        app = ScoutHud(s)
        for rel in ("", "does not exist.md", "02 - Work", "\0bad.md",
                    "10 - Personal CRM/Ali.md", "x" * 800 + ".md"):
            app.session.progress(Ev("tool", tool="vault_read", rel=rel))
        time.sleep(0.4)
        eq(app.session.frames, [])
        eq(app.session.state()[0], "idle", "no fault, and not busy")
        ok(app.scout.thread.is_alive(), "the scout survived all of it")
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_notes_screenshots_keep_the_notes_order():
    """Newest *batch* first, document order within it. A bug report's four
    screenshots are a sequence: opening the panel on the last one, with the
    filmstrip running right to left, is not what the note says."""
    root, s = make_vault()
    app = None
    try:
        for i in range(3):
            png(root / "attachments" / f"step{i}.png")
        (root / "note.md").write_text(
            "![[step0.png]]\n\n![[step1.png]]\n\n![[step2.png]]\n",
            encoding="utf-8")
        app = ScoutHud(s)
        app.session.progress(Ev("tool", tool="vault_read", rel="note.md"))
        frames = wait_for_frames(app.session, 3)
        eq([f.title for f in frames], ["step0", "step1", "step2"])
        # And a later batch lands in front of this one, still in its own order.
        app.session.show([media.Item(path=Path("/tmp/later-a.png"), title="a"),
                          media.Item(path=Path("/tmp/later-b.png"), title="b")])
        eq([f.title for f in app.session.frames][:2], ["a", "b"])
        eq(app.session.frames[2].title, "step0")
    finally:
        if app:
            app.scout.stop()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_frames_are_capped_newest_first():
    app = BareHud()
    for i in range(hud.N_FRAMES + 6):
        app.session.show([media.Item(path=Path(f"/tmp/frame-{i}.png"),
                                     title=f"f{i}")])
    eq(len(app.session.frames), hud.N_FRAMES)
    eq(app.session.frames[0].title, f"f{hud.N_FRAMES + 5}",
       "one arrival per batch, so the last one shown is the one on the glass")
    ok(len(app.session.media) > hud.N_FRAMES,
       "the registry outlives the strip — a picture still loading is not a 404")


@test
def test_frames_ride_the_live_payload():
    """The same channel as the aperture and the ticker, for the same reason:
    during a turn the engine thread is blocked, so a poll would queue behind it
    and the picture would arrive *with* the answer instead of before it."""
    app = BareHud()
    ok("frames" in app.session.live())
    app.session.show([media.Item(path=Path("/tmp/x.png"), title="x",
                                 caption="c", source="render")])
    row = app.session.live()["frames"][0]
    eq(set(row), {"id", "kind", "title", "caption", "source", "rel",
                  "bytes", "time"})
    eq(row["title"], "x")
    eq(row["source"], "render")


@test
def test_media_is_addressed_by_handle_not_by_path():
    """There is no filename in the request. `id` is a hash looked up in a
    registry the session filled when it decided to show the picture, so this
    endpoint cannot be pointed at a file the HUD did not already put on
    screen."""
    root, s = make_vault()
    app = BareHud()
    srv, port = stand_up(app)
    try:
        shot = png(root / "attachments/shot.png", size=200)
        app.session.show([media.Item(path=shot, title="shot")])
        fid = app.session.frames[0].id
        status, body, headers = request(port, "GET", "/api/media?id=" + fid)
        eq(status, 200)
        eq(headers.get("Content-Type"), "image/png")
        eq(body, shot.read_bytes())
        for bogus in ("", "deadbeef", "..%2f..%2fetc%2fpasswd",
                      "%2Fetc%2Fpasswd", "attachments%2Fshot.png"):
            status, _, _ = request(port, "GET", "/api/media?id=" + bogus)
            eq(status, 404, bogus)
    finally:
        srv.shutdown(); srv.server_close()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_framed_artifact_gets_its_own_policy():
    """A chart is a *document*, and the HUD's own CSP would blank it — a
    self-contained render is inline style and inline script by construction.
    So it gets `ARTIFACT_CSP`, which is still strictly tighter than what these
    files get today: `webbrowser.open()`, a real tab, no policy at all."""
    root, s = make_vault()
    app = BareHud()
    srv, port = stand_up(app)
    try:
        page = s.state_dir / "renders/chart.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<h1>hi</h1>", encoding="utf-8")
        app.session.show([media.Item(path=page, kind="page", title="chart")])
        fid = app.session.frames[0].id
        status, _, headers = request(port, "GET", "/api/media?id=" + fid)
        eq(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        ok("frame-ancestors 'self'" in csp, "or the viewport cannot show it")
        ok("'unsafe-inline'" in csp, "a self-contained chart is inline")
        ok("default-src 'none'" in csp, csp)
        ok("connect-src https://*.neshan.org" in csp, "tiles, and nothing else")
        # The exception is per-response; the page's own policy is untouched.
        _, _, headers = request(port, "GET", "/")
        ok("frame-ancestors 'none'" in headers.get("Content-Security-Policy", ""))
    finally:
        srv.shutdown(); srv.server_close()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_clearing_the_viewport_leaves_the_file_alone():
    """A frame is a view, not a delivery. Clearing says "stop showing me
    these" — the file is still where it was and the note still embeds it."""
    root, s = make_vault()
    app = BareHud()
    srv, port = stand_up(app)
    try:
        shot = png(root / "attachments/shot.png")
        app.session.show([media.Item(path=shot, title="shot")])
        status, body, _ = request(port, "POST", "/api/frames",
                                  {"action": "clear"})
        eq(status, 200)
        eq(json.loads(body)["live"]["frames"], [])
        ok(shot.is_file(), "clearing the screen deleted a file")
        status, _, _ = request(port, "POST", "/api/frames", {"action": "delete"})
        eq(status, 400, "there is one verb, and it is not that one")
    finally:
        srv.shutdown(); srv.server_close()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_clear_and_new_both_take_the_viewport():
    """The viewport is part of the screen, so `/clear` takes it; `/new` ends
    the conversation, so it takes it too."""
    for cmd in ("/clear", "/new"):
        app = BareHud()
        app.bridge = NewSessionBridge()
        app.session.show([media.Item(path=Path("/tmp/x.png"), title="x")])
        eq(len(app.session.frames), 1)
        app.command(cmd)
        eq(app.session.frames, [], cmd)


@test
def test_a_lost_frame_is_a_404_not_a_500():
    """A screenshot deleted in Obsidian while it is on screen."""
    root, s = make_vault()
    app = BareHud()
    srv, port = stand_up(app)
    try:
        shot = png(root / "attachments/gone.png")
        app.session.show([media.Item(path=shot, title="gone")])
        fid = app.session.frames[0].id
        shot.unlink()
        status, body, _ = request(port, "GET", "/api/media?id=" + fid)
        eq(status, 404)
        ok("error" in json.loads(body))
    finally:
        srv.shutdown(); srv.server_close()
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_landed_answer_hands_over_its_citations():
    """`_land`'s half of the same job. A structured answer — "where is Cafe
    A?", answered from SQL for nothing — never reaches a shell, so no
    `ShellEvent` is ever emitted and the citation is the only trace of what it
    read. Without this the viewport would be blind to exactly the cheap,
    instant questions it is best at illustrating."""
    app = BareHud()
    seen = []
    app.session.watch = lambda chosen, listed: seen.append((chosen, listed))
    app.bridge = StubBridge()
    app._land(Response(
        "Cafe A, on Keshavarz.", route="structured:places",
        citations=["[[09 - Anti-Library/05 - Locations/Cafe A|Cafe A]]",
                   "not a link"]))
    eq(seen[-1], (["09 - Anti-Library/05 - Locations/Cafe A.md"], []),
       "the junk citation is dropped, and a citation is a *chosen* note")


@test
def test_the_page_and_the_server_agree_on_the_frame_vocabulary():
    """`SOURCES` in `hud.js` labels every provenance the server can send. One
    the page has never heard of draws as a blank chip — and provenance is the
    whole point of the caption: a chart V drew from vault numbers and a
    screenshot pasted into a note two months ago are not the same claim."""
    js = (hud.ASSETS / "hud.js").read_text(encoding="utf-8")
    for source in ("note", "render", "place"):
        ok(f"  {source}:" in js, f"hud.js has no label for {source!r}")
    ok("/api/media?id=" in js, "the page fetches frames by handle")
    ok("sandbox: 'allow-scripts'" in js, "a framed artifact must stay sandboxed")


@test
def test_the_header_carries_the_calendar_parsa_lives_on():
    """Two clocks, one instant. The time is V's — pinned to Asia/Tehran, for
    the reason the clock comment gives — and the date beside it is the one
    the year is actually counted in.

    The claim being fixed here is the split: the Jalali date is arithmetic,
    so it is recomputed on every snapshot and cannot fail, and only the
    holiday flag is behind a cache and a network. With the service off the
    date is still on screen and `holiday` is None — which the client draws as
    a "?" rather than as a working day, because a dashboard that says "not a
    holiday" when it simply could not ask is worse than one that admits it."""
    root, s = make_vault()
    try:
        index.rebuild(s)
        from v.core.engine import Engine
        engine = Engine(s, surface=persona.HUD)
        try:
            caches = {"mail": {"at": time.time() + 1e6,
                               "val": {"state": "off", "items": []}}}
            snap = hud.snapshot(engine, hud.Session(), caches)
            j = snap["jalali"]
            eq(j["iso"], hud.jalali_mod.convert(dt.date.today()).iso)
            eq(j["gregorian"], dt.date.today().isoformat())
            ok(j["month_key"] in j["iso"], "the Category Snapshot key")
            eq(j["holiday"], None,
               "BRAIN_NO_JALALI is set for the suite — unknown, not False")
            eq(j["off"], j["friday"],
               "with no service, off falls back to the weekend and no further")
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


@test
def test_the_date_survives_a_dead_calendar_service():
    """`_jalali` is the second panel that is a network round trip, so it gets
    `_mail`'s treatment — cached, and never able to take the page down. The
    difference is that half of it does not need the network at all, and that
    half must still be there."""
    import unittest.mock as mock
    import httpx as httpx_mod
    friday, cache = dt.date(2026, 9, 11), {}
    with mock.patch.dict(os.environ, {hud.jalali_mod.NO_NETWORK_ENV: ""}), \
            mock.patch("httpx.get",
                       side_effect=httpx_mod.ConnectError("no route")):
        out = hud._jalali(friday, cache)
    eq(out["short"], "۲۰ شهریور ۱۴۰۵", "the date is arithmetic and survives")
    eq(out["holiday"], None, "the flag is absent, not false")
    eq(out["off"], True, "…and a Friday is still known to be a Friday")
    eq(cache, {}, "a failure must not be cached as an answer")


@test
def test_the_holiday_flag_is_cached_but_the_date_is_not():
    """The flag changes once a day and the date changes never, so only the
    flag is behind `JALALI_TTL`. Recomputing the date every poll is what
    keeps it right at midnight without waiting six hours for the cache."""
    import unittest.mock as mock
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "status": True,
        "result": {"solar": {"day": 1, "month": 1, "year": 1405},
                   "gregorian": {"day": 21, "month": 3, "year": 2026},
                   "holiday": True, "event": ["جشن نوروز/جشن سال نو"]}}
    nowruz, cache = dt.date(2026, 3, 21), {}
    with mock.patch.dict(os.environ, {hud.jalali_mod.NO_NETWORK_ENV: ""}), \
            mock.patch("httpx.get", return_value=resp) as got:
        first = hud._jalali(nowruz, cache)
        second = hud._jalali(nowruz, cache)
        eq(got.call_count, 1, "the second poll must read the cache")
        # A different day is a different question, so the cache must not
        # answer it — this is the bug where the HUD keeps yesterday's
        # holiday badge through midnight.
        hud._jalali(nowruz + dt.timedelta(days=1), cache)
        eq(got.call_count, 2, "a new day re-asks")
    eq(first["holiday"], True)
    eq(second["events"], ["جشن نوروز/جشن سال نو"])
    eq(first["short"], "۱ فروردین ۱۴۰۵")


# ---------------------------------------------------------------------------
# the place map


def placey_hud():
    """A real engine over a vault with a home, a café visited twice, and a
    place nobody ever gave a location."""
    root, s = make_vault()
    loc = root / "09 - Anti-Library/05 - Locations"
    (loc / "Home.md").write_text(
        '---\ntype: place\ncategory: house\nlocation: "35.70,51.40"\n---\n',
        encoding="utf-8")
    (loc / "Lamiz - Vanak.md").write_text(
        '---\ntype: place\nname: Lamiz\ncategory: cafe\n'
        'location: "35.7592,51.4097"\nrating: 10\nwifi: true\n---\n',
        encoding="utf-8")
    (loc / "Somewhere.md").write_text(
        '---\ntype: place\ncategory: bar\n---\n', encoding="utf-8")
    day = root / "01 - Daily Notes/2026/07 - July"
    for d in ("2026-07-10", "2026-07-23"):
        (day / f"{d}.md").write_text(f"Coffee at [[Lamiz - Vanak]] on {d}.\n",
                                     encoding="utf-8")
    # A link from a note that is not a day is not a visit.
    (root / "02 - Work/Issues/ISSUE-1 test issue.md").write_text(
        "---\ntype: issue\n---\nMet at [[Lamiz - Vanak]].\n", encoding="utf-8")
    s.git_enabled = False
    index.rebuild(s, full=True)
    from v.core.engine import Engine
    engine = Engine(s, surface=persona.HUD)
    app = BareHud()
    app.s = s
    app.bridge = DirectBridge(engine)
    return root, s, engine, app


@test
def test_the_place_view_lists_every_place_and_is_free():
    """A map is not a question: every place note is on it, including the one
    with no coordinates, which is listed as unplotted rather than dropped."""
    root, s, engine, app = placey_hud()
    try:
        out = app.places()
        by = {p["title"]: p for p in out["places"]}
        ok({"Home", "Lamiz - Vanak", "Somewhere", "Cafe A"} <= set(by),
           f"got {sorted(by)}")
        eq(by["Lamiz - Vanak"]["name"], "Lamiz",
           "the title is the note's, the name rides along")
        eq(by["Somewhere"]["lat"], None)
        ok(out["unplotted"] >= 1, "the place with no location must be counted")
        eq(out["home"]["title"], "Home")
        eq(by["Home"]["home_km"], 0.0)
        ok(5 < by["Lamiz - Vanak"]["home_km"] < 7,
           f"distance from Home: {by['Lamiz - Vanak']['home_km']}")
        eq(by["Lamiz - Vanak"]["visits"], 2, "two daily notes link to it")
        eq(by["Lamiz - Vanak"]["last_visit"], "2026-07-23")
        eq(by["Lamiz - Vanak"]["wifi"], True)
        eq(by["Lamiz - Vanak"]["category"], "cafe")
        eq(out["places"][0]["rating"], 10.0, "best-rated first")
        eq(app.session.live()["session"]["spent"], 0.0,
           "the place view must not spend anything")
    finally:
        engine.close()
        shutil.rmtree(root)


class FakeStatic:
    """`mapsearch.static_map`, counting. Returns a PNG header and nothing else
    — the cache does not decode, so that is all it needs to be."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, lat, lng, zoom, width, height, *, settings=None):
        self.calls.append((lat, lng, zoom, width, height))
        if self.fail:
            raise self.fail
        return b"\x89PNG fake tile"


def with_static(fake):
    from v.core import mapsearch
    real = mapsearch.static_map
    mapsearch.static_map = fake
    return lambda: setattr(mapsearch, "static_map", real)


@test
def test_a_tile_is_fetched_once_centred_on_its_cell():
    """The page projects pins with the same Mercator the server tiles with; a
    static image centred anywhere but the cell's middle puts every café one
    street over. And a cell is fetched once — after that it is a file."""
    from v.core import mapsearch
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    fake = FakeStatic()
    undo = with_static(fake)
    try:
        tiles = hud._Tiles(s)
        z, x, y = 14, 5266, 3223
        eq(tiles.get(z, x, y), b"\x89PNG fake tile")
        eq(tiles.get(z, x, y), b"\x89PNG fake tile")
        eq(len(fake.calls), 1, "the second read must come off disk")
        ok(tiles.path(z, x, y).is_file())
        lat, lng, zoom, w, h = fake.calls[0]
        px, py = mapsearch.mercator_px(lat, lng, zoom)
        ok(abs(px - (x + 0.5) * hud.TILE_PX) < 1e-6
           and abs(py - (y + 0.5) * hud.TILE_PX) < 1e-6,
           f"centre landed at {px:.3f},{py:.3f}")
        eq((w, h), (hud.TILE_PX + 2 * hud.TILE_PAD,) * 2,
           "fetched with the margin the page crops the logo off with")
        eq(tiles.cached(), 1)
    finally:
        undo()
        shutil.rmtree(root)


@test
def test_tiles_are_budgeted_and_a_failure_is_not_retried_at_once():
    """Dragging a map must not be able to spend a quota, and a dead network
    must not be a request per redraw."""
    from v.core import mapsearch
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    fake = FakeStatic()
    undo = with_static(fake)
    try:
        tiles = hud._Tiles(s)
        tiles._spent = [time.time()] * hud.TILE_BUDGET
        try:
            tiles.get(14, 1, 1)
            raise AssertionError("fetched past the budget")
        except hud.HudError as exc:
            eq(exc.status, 429)
        eq(fake.calls, [])

        tiles._spent = []
        fake.fail = mapsearch.MapSearchError("network down")
        for _ in range(2):
            try:
                tiles.get(14, 2, 2)
                raise AssertionError("a failed tile was served")
            except hud.HudError as exc:
                eq(exc.status, 503)
        eq(len(fake.calls), 2, "one retry inside the fetch, then none until the window passes")
        eq(tiles._spent, [], "a failed fetch is refunded")
    finally:
        undo()
        shutil.rmtree(root)


@test
def test_no_key_is_no_basemap_not_a_fault():
    root, s = make_vault()
    fake = FakeStatic()
    undo = with_static(fake)
    try:
        try:
            hud._Tiles(s).get(14, 1, 1)
            raise AssertionError("served a tile with no key")
        except hud.HudError as exc:
            eq(exc.status, 404)
        eq(fake.calls, [], "no key means no request at all")
    finally:
        undo()
        shutil.rmtree(root)


@test
def test_the_tile_route_takes_three_integers_and_nothing_else():
    """Addressed by grid cell, like `/api/media` by handle: there is no path
    in the request to traverse with, and the range is checked before the
    disk is."""
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    fake = FakeStatic()
    undo = with_static(fake)
    app = BareHud()
    app.tiles = hud._Tiles(s)
    srv, port = stand_up(app)
    try:
        for query, want in (("z=3&x=0&y=0", 400),
                            ("z=14&x=../../etc&y=0", 400),
                            ("z=14&x=-1&y=0", 400),
                            ("z=14&x=0", 400)):
            status, _, _ = request(port, "GET", "/api/tile?" + query)
            eq(status, want, query)
        status, body, head = request(port, "GET", "/api/tile?z=14&x=5266&y=3223")
        eq(status, 200)
        eq(head.get("Content-Type"), "image/png")
        ok("max-age" in head.get("Cache-Control", ""), "a cell never changes")
        eq(body, b"\x89PNG fake tile")
    finally:
        srv.shutdown(); srv.server_close()
        undo()
        shutil.rmtree(root)


@test
def test_the_page_and_the_server_agree_on_the_tile_grid():
    """The page's fallback grid is what it draws with when a server predating
    the `tile` field answers — it must be the server's grid, or every tile
    lands in the wrong square."""
    js = (Path(hud.ASSETS) / "hud.js").read_text(encoding="utf-8")
    import re
    m = re.search(r"return t \|\| \{ px: (\d+), pad: (\d+), zmin: (\d+), zmax: (\d+) \}", js)
    ok(m, "could not find the page's fallback tile grid")
    eq(tuple(int(v) for v in m.groups()),
       (hud.TILE_PX, hud.TILE_PAD, *hud.TILE_ZOOMS))
    ok("{ id: 'places'" in js, "the PLACES view is missing from the nav")


# --- rates ---------------------------------------------------------------
def _rates_hud(key: str = "free-TEST-KEY"):
    """A `BareHud` with a real `rates.Rates` on a throwaway state dir."""
    import tempfile
    from v.core import config, rates as rates_mod
    root = Path(tempfile.mkdtemp(prefix="v-hud-rates-"))
    s = config.Settings(vault=root, state_dir=root / "state")
    s.navasan_api_key = key
    s.state_dir.mkdir(parents=True)
    app = BareHud()
    app.s = s
    app.rates = rates_mod.Rates(s)
    app._rates_kicked = 0.0
    app._rates_thread = None
    return root, app


class _Navasan:
    """`httpx.get`, answering with a board and counting the calls."""

    def __init__(self, status=200):
        self.status, self.calls = status, 0

    def __call__(self, url, params=None, **kw):
        self.calls += 1
        row = {"change": 500, "timestamp": 1791270000,
               "date": "1405-07-14 12:00:00"}
        body = {k: {**row, "value": v} for k, v in (
            ("usd_sell", "104500"), ("eur", "121300"),
            ("18ayar", "9150000"), ("sekkeh", "98500000"))}

        class R:
            status_code = self.status
            text = "x"

            @staticmethod
            def json():
                return body
        return R()


@test
def test_rates_card_states_are_distinct():
    """Not set up, nothing yet, and a board are three panels. A card that
    draws "no key" and "no data" the same way hides the one that needs a
    hand from the one that will fix itself."""
    import unittest.mock as mock
    root, app = _rates_hud(key="")
    try:
        eq(hud._rates(app.s, dt.datetime(2026, 10, 6, 9))["state"],
           "unconfigured")
        app.s.navasan_api_key = "free-TEST-KEY"
        panel = hud._rates(app.s, dt.datetime(2026, 10, 6, 9))
        eq(panel["state"], "empty")
        ok(panel["budget"]["can_refresh"], "an empty board can be fetched")
        fake = _Navasan()
        with mock.patch("httpx.get", fake):
            app.rates.ensure_today(dt.datetime(2026, 10, 6, 9))
        panel = hud._rates(app.s, dt.datetime(2026, 10, 6, 9, 5))
        eq(panel["state"], "on")
        ok(panel["today"])
        eq([i["key"] for i in panel["items"]],
           ["usd_sell", "eur", "18ayar", "sekkeh"], "the card's order")
        eq(panel["items"][0]["spark"], [104500.0])
        ok(not panel["budget"]["can_refresh"], "the cooldown reaches the card")
        ok("09:30" in panel["budget"]["why_not"], panel["budget"]["why_not"])
        stale = hud._rates(app.s, dt.datetime(2026, 10, 7, 9))
        ok(stale["state"] == "on" and not stale["today"],
           "yesterday's board is drawn as yesterday's")
    finally:
        shutil.rmtree(root)


@test
def test_rates_endpoint_refuses_with_the_reason():
    """The ↻ spends a request against a monthly quota, so the endpoint takes
    one verb with no default, and a budget refusal comes back as a 429 the
    page can print — "not now, and when" is an answer, not an error."""
    import unittest.mock as mock
    root, app = _rates_hud()
    srv, port = stand_up(app)
    try:
        fake = _Navasan()
        with mock.patch("httpx.get", fake):
            status, raw, _ = request(port, "POST", "/api/rates", {})
            eq(status, 400, "no action, no request")
            status, raw, _ = request(port, "POST", "/api/rates",
                                     {"action": "refresh"})
            eq(status, 200, raw)
            out = json.loads(raw)
            eq(out["rates"]["state"], "on")
            status, raw, _ = request(port, "POST", "/api/rates",
                                     {"action": "refresh"})
            eq(status, 429, "a second click inside the cooldown")
            ok("refresh opens at" in json.loads(raw)["error"])
        eq(fake.calls, 1, "and only the first one reached Navasan")
        status, raw, _ = request(port, "POST", "/api/rates",
                                 {"action": "refresh"},
                                 headers={"Sec-Fetch-Site": "cross-site"})
        eq(status, 403, "another tab cannot spend the quota")
    finally:
        srv.shutdown(); srv.server_close()
        shutil.rmtree(root)
    bare = BareHud()
    srv, port = stand_up(bare)
    try:
        status, _, _ = request(port, "POST", "/api/rates", {"action": "refresh"})
        eq(status, 503, "a server with no rates says so")
    finally:
        srv.shutdown(); srv.server_close()


@test
def test_the_days_fetch_is_kicked_off_the_snapshot_thread():
    """The HUD opening is the first look of the day, so it is what spends the
    day's request — on its own thread, once, and never again that day
    however often the page polls."""
    import unittest.mock as mock
    root, app = _rates_hud()
    try:
        fake = _Navasan()
        with mock.patch("httpx.get", fake):
            app._kick_rates()
            ok(app._rates_thread is not None, "a fetch was started")
            ok(app._rates_thread is not threading.current_thread())
            app._rates_thread.join(5)
            for _ in range(5):
                app._rates_kicked = 0.0          # as if minutes had passed
                app._kick_rates()
                if app._rates_thread:
                    app._rates_thread.join(5)
        eq(fake.calls, 1)
        ok(app.rates.board() is not None)
    finally:
        shutil.rmtree(root)
    BareHud()._kick_rates()            # no `rates` at all: a no-op, not a crash


@test
def test_the_page_carries_the_rates_card():
    html = (Path(hud.ASSETS) / "index.html").read_text(encoding="utf-8")
    js = (Path(hud.ASSETS) / "hud.js").read_text(encoding="utf-8")
    ok('id="p-rates"' in html and 'id="rates-refresh"' in html)
    ok("post('/api/rates', { action: 'refresh' })" in js,
       "the button sends the one verb the server takes")
    ok("paintRates(app.snap)" in js, "and the card is painted on every frame")


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
