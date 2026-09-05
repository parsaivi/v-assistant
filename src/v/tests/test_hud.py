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
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import hud, index, persona
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
    """A `shells.ShellEvent`, minus the import — the progress callback only
    ever reads these four fields."""

    def __init__(self, kind, tool="", detail="", n=0):
        self.kind, self.tool, self.detail, self.n = kind, tool, detail, n


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
            for key in ("vault", "gauges", "day_events", "tasks", "deadlines",
                        "mems", "nudges", "mail", "alarm", "agent", "header"):
                ok(key in snap, f"snapshot lost {key}")
            eq(snap["agent"], "v")
            ok(snap["vault"]["notes"] > 0)
            ok(isinstance(snap["gauges"]["drift"]["files"], int))
        finally:
            engine.close()
    finally:
        shutil.rmtree(root)


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
