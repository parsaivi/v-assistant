"""Tests for the zero-cost core.

Run:  python3 -m v.tests.test_core

No pytest dependency — this is a vault, not a Python project, and the suite
should run with nothing installed. Every test builds a throwaway vault in a
temp dir, so none of them can touch the real one.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import os
# Never let a test spend plan allowance or 9s on a model call. The
# decider ships enabled, and every fixture here builds a real
# Settings, so this has to be set before v.core is imported.
os.environ["BRAIN_NO_MODEL"] = "1"
# …and never pop a desktop notification at whoever is running the suite. The
# scheduler tests really do fire the morning briefing.
os.environ["BRAIN_NO_NOTIFY"] = "1"
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import (capture, config, embed, gmail, index, llm,
                            mapsearch, notes, nudges, query, quera, rag,
                            reminders, render, router, schema, vaultfs)
from v.core.engine import Engine

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


# ---------------------------------------------------------------------
def make_vault() -> tuple[Path, config.Settings]:
    root = Path(tempfile.mkdtemp(prefix="brain-test-"))
    (root / "01 - Daily Notes/2026/07 - July").mkdir(parents=True)
    (root / "02 - Work/Issues").mkdir(parents=True)
    (root / "04 - Gym/Workouts").mkdir(parents=True)
    (root / "09 - Anti-Library/05 - Locations").mkdir(parents=True)
    (root / "10 - Personal CRM").mkdir(parents=True)
    (root / "14 - Music/Lyrics").mkdir(parents=True)

    (root / "02 - Work/Issues/ISSUE-1 test issue.md").write_text(
        '---\ntype: issue\nissue-id: 1\ntitle: test issue\nproject: Pricing\n'
        'status: in-review\npriority: high\ncreated: "2026-07-01"\n---\n\n'
        "Body about pricing and discounts.\n\n## Notes\n\nSome detail.\n",
        encoding="utf-8")
    (root / "02 - Work/Issues/ISSUE-2 broken.md").write_text(
        '---\ntype: issue\nissue-id: 2\ntitle: broken\nproject: Pricing\n'
        'status: opened\npriority: low\ncreated: "2026-07-20"\n---\n\nBody.\n',
        encoding="utf-8")
    (root / "09 - Anti-Library/05 - Locations/Cafe A.md").write_text(
        '---\ntype: place\nname: Cafe A\ncategory: cafe\nlocation: "1,2"\n'
        'city: Tehran\nrating: 9\nwifi: true\nnoise: low\noutdoor-seating: true\n'
        '---\n\nGood coffee.\n', encoding="utf-8")
    (root / "10 - Personal CRM/Ali.md").write_text(
        '---\ntype: person\ntier: 1\nfrequency_days: 7\n'
        'last_contact: "2026-01-01"\n---\n\n## Interactions\n',
        encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    # `model_dir` as well as `state_dir`, and it matters more than it looks:
    # before the package split, `tts.py` derived the model path from
    # `state_dir.parent`, so a throwaway vault got a throwaway model dir for
    # free. Now that models live in ~/.cache/v, a test that forgets this reads
    # the real 12 GB cache — and the "no voice installed" tests pass or fail
    # depending on what the machine happens to have downloaded.
    s = config.Settings(vault=root, state_dir=root / ".state",
                        model_dir=root / ".models")
    s.ensure_dirs()
    index.rebuild(s, full=True)
    return root, s


# --- notes -----------------------------------------------------------
@test
def frontmatter_roundtrip():
    n = notes.parse_text('---\ntype: issue\nstatus: open\ncreated: "2026-07-01"\n'
                         "---\n\nBody here.\n")
    eq(n.meta["type"], "issue")
    eq(n.meta["status"], "open")
    ok("Body here" in n.body)
    ok('created: "2026-07-01"' in n.render(), "dates must stay quoted")


@test
def wikilinks_ignore_code():
    n = notes.parse_text("Real [[A/B]] and folder [[C/]].\n\n"
                         "```dataview\n" + 'x = "[[" + dv.current() + "]]"\n' +
                         "```\n`[[inline]]`\n")
    eq(n.links, ["A/B"], "code blocks and folder links must not become links")


@test
def section_extraction():
    n = notes.parse_text("# T\n\n## One\n\nalpha\n\n## Two\n\nbeta\n")
    eq(n.section("One"), "alpha")
    eq(n.section("Two"), "beta")
    eq(n.section("Missing"), None)


@test
def date_coercion():
    eq(notes.as_date('"2026-07-31"'), dt.date(2026, 7, 31))
    eq(notes.as_date("2026-07-31"), dt.date(2026, 7, 31))
    eq(notes.as_date(None), None)
    eq(notes.as_date("not a date"), None)


# --- index -----------------------------------------------------------
@test
def index_is_incremental():
    root, s = make_vault()
    try:
        st = index.rebuild(s)
        eq(st.updated, 0, "nothing changed, so nothing should re-parse")
        (root / "02 - Work/Issues/ISSUE-1 test issue.md").write_text(
            '---\ntype: issue\nissue-id: 1\ntitle: test issue\nproject: Pricing\n'
            'status: closed\npriority: high\ncreated: "2026-07-01"\n---\n\nNew.\n',
            encoding="utf-8")
        st = index.rebuild(s)
        eq(st.updated, 1)
    finally:
        shutil.rmtree(root)


@test
def chunk_ids_survive_reindex():
    """The bug that decoupled embeddings from notes. Must never regress."""
    root, s = make_vault()
    try:
        conn = index.connect(s)
        before = {r["id"]: r["text"] for r in
                  conn.execute("SELECT id, text FROM chunks")}
        conn.close()
        index.rebuild(s, full=True)
        conn = index.connect(s)
        after = {r["id"]: r["text"] for r in
                 conn.execute("SELECT id, text FROM chunks")}
        conn.close()
        eq(after, before, "chunk ids and text must be stable across a rebuild")
    finally:
        shutil.rmtree(root)


@test
def a_note_v_wrote_is_queryable_without_a_full_rebuild():
    """Tasks live only in the mirror — nothing queries the markdown — so a
    write that skips the mirror produces a task that exists on disk and is
    invisible to every question asked about it."""
    root, s = make_vault()
    try:
        conn = index.connect(s)
        rel = "01 - Daily Notes/2026/07 - July/2026-07-12.md"
        before = index.stats(s)["notes"]
        (root / rel).write_text(
            '---\ntype: daily\n---\n\n## Tasks\n\n- [ ] call the dentist\n',
            encoding="utf-8")
        index.resync_note(conn, rel, s)

        rows = conn.execute("SELECT text FROM tasks WHERE rel=?", (rel,)).fetchall()
        ok(any("dentist" in r["text"] for r in rows),
           "the task must be in the mirror immediately after the write")
        eq(index.stats(s)["notes"], before + 1, "and the note with it")
        ok(not index.is_stale(s, conn),
           "a resynced note must not still read as stale — that is what "
           "made every following call order a pointless full rebuild")
        conn.close()
    finally:
        shutil.rmtree(root)


@test
def staleness_is_per_note_not_a_global_timestamp():
    root, s = make_vault()
    try:
        conn = index.connect(s)
        ok(not index.is_stale(s, conn), "a freshly built index is not stale")
        p = root / "01 - Daily Notes/2026/07 - July/2026-07-13.md"
        p.write_text("---\ntype: daily\n---\n\nwritten in Obsidian\n",
                     encoding="utf-8")
        ok(index.is_stale(s, conn), "a new note makes it stale")
        index.refresh_if_stale(s, conn)
        ok(not index.is_stale(s, conn), "and refreshing clears it")
        p.unlink()
        ok(index.is_stale(s, conn),
           "a deletion leaves every surviving mtime untouched, so the count "
           "has to be what catches it")
        conn.close()
    finally:
        shutil.rmtree(root)


# --- schema ----------------------------------------------------------
@test
def validator_flags_gitlab_status_as_a_dashboard_problem_not_an_error():
    """`opened` is still reported — but as a warning, and it says why.

    It was an error, which meant 18 of them every day. V now normalises the
    value, so the note is *not* invisible to V; it is only invisible to
    Dataview, which compares raw strings. Reporting a problem V handles at the
    same severity as a note that failed to parse trains you to ignore the
    validator, and then the real errors go unread too.
    """
    root, s = make_vault()
    try:
        report = schema.validate_vault(s)
        issues = [i for i in report.by_file().get(
            "02 - Work/Issues/ISSUE-2 broken.md", []) if i.field == "status"]
        eq(len(issues), 1, "`opened` must still be reported")
        eq(issues[0].level, "warn", "…but not as a hard error")
        ok("dashboards" in issues[0].message,
           "the report must say what actually breaks")
        ok(not [i for i in report.errors
                if i.field == "status" and "ISSUE-2" in i.rel],
           "and it must not be counted among the errors")
    finally:
        shutil.rmtree(root)


@test
def validator_catches_template_tokens():
    n = notes.parse_text('---\ntype: workout\ndate: "{{date:YYYY-MM-DD}}"\n'
                         "type-of-training: Push\n---\n\nx\n")
    issues = schema.validate_note(n)
    ok(any("template token" in i.message for i in issues))


# --- queries ---------------------------------------------------------
@test
def structured_queries_are_exact():
    root, s = make_vault()
    try:
        q = query.QueryEngine(s)
        eq(len(q.issues(status="in-review").rows), 1)
        eq(len(q.stale_reviews(days=5).rows), 1)
        eq(len(q.places(category="cafe", wifi=True).rows), 1)
        eq(len(q.places(category="cafe", wifi=False).rows), 0)
        eq(len(q.overdue_contacts().rows), 1)
        q.close()
    finally:
        shutil.rmtree(root)


@test
def places_near_ranks_and_filters_by_distance():
    root, s = make_vault()
    try:
        (root / "09 - Anti-Library/05 - Locations/Home.md").write_text(
            '---\ntype: place\nname: Home\ncategory: house\n'
            'location: "1.001,2.001"\n---\n\nHome.\n', encoding="utf-8")
        (root / "09 - Anti-Library/05 - Locations/Far Place.md").write_text(
            '---\ntype: place\nname: Far Place\ncategory: restaurant\n'
            'location: "10,10"\n---\n\nFar away.\n', encoding="utf-8")
        index.rebuild(s, full=True)
        q = query.QueryEngine(s)

        anchor = q.resolve_anchor("home")
        ok(anchor is not None, "must resolve a place note case-insensitively")
        eq(anchor[2], "Home")
        ok(q.resolve_anchor("nowhere near a real place") is None)

        near_cafes = q.places(category="cafe", near="Home", radius_km=1)
        eq([r.title for r in near_cafes.rows], ["Cafe A"],
           "category filter still applies; Far Place (1000km+) is outside 1km")
        ok(near_cafes.rows[0].extra["distance_km"] < 1)

        ranked = q.places(near="Home")
        eq([r.title for r in ranked.rows], ["Home", "Cafe A", "Far Place"],
           "nearest first, no radius cutoff — includes the anchor itself at 0km")

        unknown = q.places(near="Nonexistent Place XYZ")
        eq(unknown.rows, [])
        ok("Nonexistent Place XYZ" in unknown.answer)
        q.close()
    finally:
        shutil.rmtree(root)


@test
def haversine_distance_matches_known_geometry():
    eq(query.haversine_km(35.7, 51.4, 35.7, 51.4), 0.0)
    # One degree of latitude, and one degree of longitude at the equator,
    # are both ~111.19km along a great circle — a fixed, checkable fact
    # rather than a real-world city pair that could be eyeballed wrong.
    ok(110 < query.haversine_km(0, 0, 0, 1) < 112)
    ok(110 < query.haversine_km(0, 0, 1, 0) < 112)


@test
def mapsearch_returns_none_without_an_api_key():
    root, s = make_vault()
    try:
        eq(s.neshan_api_key, "", "fixture settings must not pick up a real key")
        ok(mapsearch.search("cafe", 35.7, 51.4, settings=s) is None,
           "no key configured means 'not available', not an empty result")
    finally:
        shutil.rmtree(root)


@test
def mapsearch_parses_hits_and_ranks_by_distance():
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        payload = {"items": [
            {"title": "Far Cafe", "address": "far away",
             "location": {"x": 51.5, "y": 35.8}},
            {"title": "Near Cafe", "address": "close by", "type": "cafe",
             "location": {"x": 51.401, "y": 35.701}},
            {"title": "No coords"},
        ]}
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch("httpx.get", return_value=resp) as got:
            hits = mapsearch.search("cafe", 35.7, 51.4, settings=s)
        got.assert_called_once()
        ok(got.call_args.kwargs["headers"]["Api-Key"] == "test-key")
        eq([h.title for h in hits], ["Near Cafe", "Far Cafe"],
           "nearest first; the hit with no location must be dropped, not crash")
        ok(hits[0].distance_km < hits[1].distance_km)
    finally:
        shutil.rmtree(root)


@test
def polyline_decoding_matches_the_published_reference_vector():
    """Google's own worked example from the encoded-polyline spec, which is
    the format Neshan emits and OpenLayers reads. Pinned to the reference
    rather than to a Neshan response so a vendor outage can't quietly turn
    this into a test of nothing."""
    pts = mapsearch.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    eq([(round(a, 5), round(b, 5)) for a, b in pts],
       [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])
    eq(mapsearch.decode_polyline(""), [], "an empty polyline is not a crash")
    # A truncated payload is what a clipped response looks like; keep the
    # prefix that parsed rather than losing the whole route to a ValueError.
    ok(len(mapsearch.decode_polyline("_p~iF~ps|U_ulLnnqC_mqN")) >= 2)


@test
def route_returns_none_without_an_api_key():
    root, s = make_vault()
    try:
        eq(s.neshan_api_key, "", "fixture settings must not pick up a real key")
        ok(mapsearch.route((35.7, 51.4), (35.8, 51.4), settings=s) is None,
           "no key means 'not available', which the tool turns into 'say it "
           "is an estimate' — not a silently fabricated duration")
    finally:
        shutil.rmtree(root)


@test
def route_sums_legs_and_avoids_tehrans_restricted_zones():
    """Two things at once, because they fail the same way — silently, with a
    number that looks right. A waypointed route reports per leg, so reading
    `legs[0]` answers a question nobody asked; and a route through طرح ترافیک
    is one Parsa cannot legally drive, which is the single capability no
    other vendor offers and the reason Neshan stays primary."""
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        payload = {"routes": [{
            "overview_polyline": {"points": "_p~iF~ps|U_ulLnnqC"},
            "legs": [
                {"summary": "ولیعصر", "distance": {"value": 4000.0},
                 "duration": {"value": 600.0}},
                {"summary": "تجریش", "distance": {"value": 2500.0},
                 "duration": {"value": 450.0}},
            ]}]}
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch("httpx.get", return_value=resp) as got:
            r = mapsearch.route((35.6997, 51.3376), (35.8044, 51.4337),
                                origin_label="Divar", dest_label="Rooberoo",
                                settings=s)
        params = got.call_args.kwargs["params"]
        eq(got.call_args.kwargs["headers"]["Api-Key"], "test-key")
        eq(params["avoidTrafficZone"], "true", "طرح ترافیک must be avoided")
        eq(params["avoidOddEvenZone"], "true", "زوج/فرد must be avoided")
        eq(params["origin"], "35.6997,51.3376")
        eq(r.distance_m, 6500.0, "both legs, not just the first")
        eq(r.duration_s, 1050.0)
        eq(r.distance_km, 6.5)
        eq(r.duration_min, 18)
        eq(r.summary, "ولیعصر · تجریش")
        eq(r.polyline, "_p~iF~ps|U_ulLnnqC",
           "the encoded string is kept verbatim — OpenLayers decodes it")
        ok(len(r.points) == 2, "and decoded too, for centring the map")
    finally:
        shutil.rmtree(root)


@test
def route_refuses_the_modes_neshan_cannot_do():
    """The failure this prevents is a metro question answered with a car
    route wearing the authority of a measurement. Neshan has no transit mode
    at any tier, so the honest move is to refuse rather than substitute."""
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        for mode in ("transit", "walking", "bicycle"):
            try:
                mapsearch.route((35.7, 51.4), (35.8, 51.4), mode=mode,
                                settings=s)
                raise AssertionError(f"{mode} should not have been routed")
            except mapsearch.MapSearchError as exc:
                ok(mode in str(exc), "the error must name the mode refused")
    finally:
        shutil.rmtree(root)


@test
def a_place_name_cannot_break_out_of_the_map_pages_javascript():
    """Place labels are vault prose and land inside a <script>. An apostrophe
    in "Sam's Cafe" is the benign version; `</script>` is the other one."""
    points = [{"label": "Sam's Cafe </script><img onerror=x>", "lat": 35.7,
               "lng": 51.4, "category": "cafe"},
              {"label": 'He said "hi"', "lat": 35.8, "lng": 51.5}]
    page = render.place_html(points, web_key="web-key-123")
    tail = page.split("<script>")[-1]
    ok("</script><img" not in tail, "the closing tag must be escaped in JS")
    ok("web-key-123" in page, "the web key is embedded — it has to be")


@test
def route_prefers_step_geometry_over_the_coarse_overview():
    """`overview_polyline` is a simplification — 22 points for a 7km Tehran
    route, which visibly cuts corners when drawn over real streets, where the
    per-step polylines give 210. Drawing the summary shape and calling it the
    route is the kind of wrong that looks fine until you follow it."""
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        payload = {"routes": [{
            "overview_polyline": {"points": "_p~iF~ps|U"},          # 1 point
            "legs": [{"distance": {"value": 100.0}, "duration": {"value": 60.0},
                      "steps": [{"polyline": "_p~iF~ps|U_ulLnnqC"},   # 2
                                {"polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}]}]}]}
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch("httpx.get", return_value=resp):
            r = mapsearch.route((35.7, 51.4), (35.8, 51.4), settings=s)
        eq(len(r.points), 5, "every step's geometry, concatenated")
        eq(r.polyline, "_p~iF~ps|U",
           "the overview is still carried, just not what gets drawn")

        # A response with legs but no steps must still draw something.
        payload["routes"][0]["legs"][0]["steps"] = []
        with mock.patch("httpx.get", return_value=resp):
            r2 = mapsearch.route((35.7, 51.4), (35.8, 51.4), settings=s)
        eq(len(r2.points), 1, "falls back to the overview rather than nothing")
    finally:
        shutil.rmtree(root)


@test
def the_route_png_lands_on_the_tiles_it_asked_for():
    """The PNG is a composite: Neshan draws the streets, this draws the route
    on top, and nothing checks they agree. They agree only if the Mercator
    maths here matches the tiles' own — so pin the projection, and pin that
    the centre of the fetched image is the centre of the route's bounding box.
    """
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        # Zoom 0 is one 256px tile of the whole world; (0,0) sits dead centre.
        x, y = mapsearch.mercator_px(0.0, 0.0, 0)
        eq((round(x, 6), round(y, 6)), (128.0, 128.0))
        # Doubling the zoom doubles the world.
        x1, _ = mapsearch.mercator_px(0.0, 180.0, 1)
        eq(round(x1, 6), 512.0)

        pts = [(35.76, 51.41), (35.80, 51.43)]
        r = mapsearch.Route("A", "B", "car", 1000.0, 600.0, "", "", pts)
        asked = {}

        def fake_static(lat, lng, zoom, width, height, settings=None):
            asked.update(lat=lat, lng=lng, zoom=zoom, w=width, h=height)
            from PIL import Image
            import io
            buf = io.BytesIO()
            Image.new("RGBA", (width, height), (200, 200, 200, 255)).save(buf, "PNG")
            return buf.getvalue()

        with mock.patch.object(mapsearch, "static_map", fake_static):
            png = render.route_png(r, settings=s)
        ok(png.startswith(b"\x89PNG"), "a PNG, not an HTML error page")
        eq(round(asked["lat"], 4), 35.78, "centred on the route, not an endpoint")
        eq(round(asked["lng"], 4), 51.42)
        ok(5 <= asked["zoom"] <= 19, "Neshan rejects zoom outside 5-19")

        # No key means no tiles means no picture — never a broken one.
        s.neshan_api_key = ""
        ok(render.route_png(r, settings=s) is None)
    finally:
        shutil.rmtree(root)


@test
def directions_open_a_real_map_because_neshan_cannot_be_deep_linked():
    """neshan.org rewrites its own URL and then ignores it on a cold load —
    both `/routing/car/origin/<latlng>/destination/<latlng>` and a bare
    `#c<lat>-<lng>-<zoom>z` reset to Tehran centre, verified in a browser on
    2026-09-02. Google's documented `api=1` form is what's left, and it needs
    no key. Pinned so a "tidy-up" back to the path form gets caught."""
    url = mapsearch.directions_url((35.6997, 51.3376), (35.8044, 51.4337))
    ok(url.startswith("https://www.google.com/maps/dir/?api=1"), url)
    ok("origin=35.6997,51.3376" in url and "destination=35.8044,51.4337" in url)
    ok("travelmode=driving" in url)
    ok("walking" in mapsearch.directions_url((0, 0), (1, 1), mode="walking"))
    # Motorcycle has no Google equivalent; it must not silently vanish.
    ok("driving" in mapsearch.directions_url((0, 0), (1, 1), mode="motorcycle"))


@test
def route_prefers_step_geometry_over_the_coarse_overview():
    """`overview_polyline` is a simplification — 22 points for a 7km Tehran
    route, which visibly cuts corners when drawn over real streets, where the
    per-step polylines give 210. Drawing the summary shape and calling it the
    route is the kind of wrong that looks fine until you follow it."""
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        payload = {"routes": [{
            "overview_polyline": {"points": "_p~iF~ps|U"},          # 1 point
            "legs": [{"distance": {"value": 100.0}, "duration": {"value": 60.0},
                      "steps": [{"polyline": "_p~iF~ps|U_ulLnnqC"},   # 2
                                {"polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}]}]}]}
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch("httpx.get", return_value=resp):
            r = mapsearch.route((35.7, 51.4), (35.8, 51.4), settings=s)
        eq(len(r.points), 5, "every step's geometry, concatenated")
        eq(r.polyline, "_p~iF~ps|U",
           "the overview is still carried, just not what gets drawn")

        # A response with legs but no steps must still draw something.
        payload["routes"][0]["legs"][0]["steps"] = []
        with mock.patch("httpx.get", return_value=resp):
            r2 = mapsearch.route((35.7, 51.4), (35.8, 51.4), settings=s)
        eq(len(r2.points), 1, "falls back to the overview rather than nothing")
    finally:
        shutil.rmtree(root)


@test
def the_route_png_lands_on_the_tiles_it_asked_for():
    """The PNG is a composite: Neshan draws the streets, this draws the route
    on top, and nothing checks they agree. They agree only if the Mercator
    maths here matches the tiles' own — so pin the projection, and pin that
    the centre of the fetched image is the centre of the route's bounding box.
    """
    import unittest.mock as mock
    root, s = make_vault()
    s.neshan_api_key = "test-key"
    try:
        # Zoom 0 is one 256px tile of the whole world; (0,0) sits dead centre.
        x, y = mapsearch.mercator_px(0.0, 0.0, 0)
        eq((round(x, 6), round(y, 6)), (128.0, 128.0))
        # Doubling the zoom doubles the world.
        x1, _ = mapsearch.mercator_px(0.0, 180.0, 1)
        eq(round(x1, 6), 512.0)

        pts = [(35.76, 51.41), (35.80, 51.43)]
        r = mapsearch.Route("A", "B", "car", 1000.0, 600.0, "", "", pts)
        asked = {}

        def fake_static(lat, lng, zoom, width, height, settings=None):
            asked.update(lat=lat, lng=lng, zoom=zoom, w=width, h=height)
            from PIL import Image
            import io
            buf = io.BytesIO()
            Image.new("RGBA", (width, height), (200, 200, 200, 255)).save(buf, "PNG")
            return buf.getvalue()

        with mock.patch.object(mapsearch, "static_map", fake_static):
            png = render.route_png(r, settings=s)
        ok(png.startswith(b"\x89PNG"), "a PNG, not an HTML error page")
        eq(round(asked["lat"], 4), 35.78, "centred on the route, not an endpoint")
        eq(round(asked["lng"], 4), 51.42)
        ok(5 <= asked["zoom"] <= 19, "Neshan rejects zoom outside 5-19")

        # No key means no tiles means no picture — never a broken one.
        s.neshan_api_key = ""
        ok(render.route_png(r, settings=s) is None)
    finally:
        shutil.rmtree(root)


@test
def directions_open_a_real_map_because_neshan_cannot_be_deep_linked():
    """neshan.org rewrites its own URL and then ignores it on a cold load —
    both `/routing/car/origin/<latlng>/destination/<latlng>` and a bare
    `#c<lat>-<lng>-<zoom>z` reset to Tehran centre, verified in a browser on
    2026-09-02. Google's documented `api=1` form is what's left, and it needs
    no key. Pinned so a "tidy-up" back to the path form gets caught."""
    url = mapsearch.directions_url((35.6997, 51.3376), (35.8044, 51.4337))
    ok(url.startswith("https://www.google.com/maps/dir/?api=1"), url)
    ok("origin=35.6997,51.3376" in url and "destination=35.8044,51.4337" in url)
    ok("travelmode=driving" in url)
    ok("walking" in mapsearch.directions_url((0, 0), (1, 1), mode="walking"))
    # Motorcycle has no Google equivalent; it must not silently vanish.
    ok("driving" in mapsearch.directions_url((0, 0), (1, 1), mode="motorcycle"))



# --- gmail -------------------------------------------------------------
class _FakeHTTPResponse:
    """Stands in for what `urllib.request.urlopen(...)` returns when used as
    a context manager — `gmail.py` never touches `requests`/`httpx` for its
    own transport, same reasoning `gcal.py` gives for staying stdlib-only."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gmail_ready(s) -> gmail.Gmail:
    g = gmail.Gmail(s)
    g._write_token({"access_token": "tok", "refresh_token": "r",
                    "expires_at": 9999999999})
    return g


@test
def gmail_no_token_raises_not_authorised():
    root, s = make_vault()
    try:
        g = gmail.Gmail(s)
        try:
            g.search("is:unread")
            ok(False, "must raise without a saved token")
        except gmail.NotAuthorised:
            pass
    finally:
        shutil.rmtree(root)


@test
def gmail_access_token_refreshes_when_expired():
    import unittest.mock as mock
    root, s = make_vault()
    os.environ["GCAL_CLIENT_ID"] = "cid"
    os.environ["GCAL_CLIENT_SECRET"] = "secret"
    try:
        g = gmail.Gmail(s)
        g._write_token({"access_token": "old", "refresh_token": "r",
                        "expires_at": 0})
        fresh = _FakeHTTPResponse(
            json.dumps({"access_token": "fresh", "expires_in": 3600}).encode())
        with mock.patch("urllib.request.urlopen", return_value=fresh):
            tok = g._access_token()
        eq(tok, "fresh")
        eq(g._read_token()["access_token"], "fresh",
           "a refreshed token must be persisted, not just returned")
    finally:
        os.environ.pop("GCAL_CLIENT_ID", None)
        os.environ.pop("GCAL_CLIENT_SECRET", None)
        shutil.rmtree(root)


@test
def gmail_search_and_read_parse_headers_and_body():
    import base64
    import unittest.mock as mock
    root, s = make_vault()
    try:
        g = _gmail_ready(s)
        list_body = json.dumps({"messages": [{"id": "m1"}]}).encode()
        meta_body = json.dumps({
            "id": "m1", "threadId": "t1", "snippet": "hi there",
            "labelIds": ["UNREAD"],
            "payload": {"headers": [
                {"name": "From", "value": "arash@x.com"},
                {"name": "Subject", "value": "hello"},
                {"name": "Date", "value": "Mon, 10 Aug 2026 09:00:00 +0000"},
            ]},
        }).encode()
        raw = base64.urlsafe_b64encode(b"plain body text").decode()
        full_body = json.dumps({
            "id": "m1", "threadId": "t1", "snippet": "hi there",
            "labelIds": ["UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "arash@x.com"},
                    {"name": "To", "value": "parsa@x.com"},
                    {"name": "Subject", "value": "hello"},
                    {"name": "Date", "value": "Mon, 10 Aug 2026 09:00:00 +0000"},
                ],
                "mimeType": "text/plain", "body": {"data": raw},
            },
        }).encode()

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "format=full" in url:
                return _FakeHTTPResponse(full_body)
            if "format=metadata" in url:
                return _FakeHTTPResponse(meta_body)
            return _FakeHTTPResponse(list_body)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            hits = g.search("is:unread")
            eq(len(hits), 1)
            eq(hits[0].subject, "hello")
            ok(hits[0].unread, "UNREAD label must set unread")

            msg = g.read("m1")
            eq(msg.body, "plain body text",
               "base64url body must be decoded, not returned raw")
            eq(msg.to, "parsa@x.com")
    finally:
        shutil.rmtree(root)


@test
def gmail_draft_never_sends_and_send_needs_a_draft_id():
    import unittest.mock as mock
    root, s = make_vault()
    try:
        g = _gmail_ready(s)
        calls = []

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            calls.append(url)
            if url.endswith("/drafts"):
                return _FakeHTTPResponse(json.dumps({"id": "d1"}).encode())
            if url.endswith("/drafts/send"):
                return _FakeHTTPResponse(json.dumps({"id": "sent1"}).encode())
            raise AssertionError(f"unexpected call: {url}")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            draft_id = g.draft("a@x.com", "subj", "body text")
            eq(draft_id, "d1")
            ok(not any(c.endswith("/drafts/send") for c in calls),
               "draft() must never reach the send endpoint on its own")

            sent_id = g.send("d1")
            eq(sent_id, "sent1")
    finally:
        shutil.rmtree(root)


@test
def gmail_unseen_dedups_across_calls():
    import unittest.mock as mock
    root, s = make_vault()
    try:
        g = _gmail_ready(s)
        list_body = json.dumps({"messages": [{"id": "m1"}, {"id": "m2"}]}).encode()

        def meta_for(mid):
            return json.dumps({
                "id": mid, "threadId": "t", "snippet": "s", "labelIds": [],
                "payload": {"headers": [{"name": "Subject", "value": mid}]},
            }).encode()

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            for mid in ("m1", "m2"):
                if f"/messages/{mid}" in url:
                    return _FakeHTTPResponse(meta_for(mid))
            return _FakeHTTPResponse(list_body)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = g.unseen("is:unread")
            eq(len(first), 2)
            second = g.unseen("is:unread")
            eq(second, [], "an id already surfaced must not be pushed twice")
    finally:
        shutil.rmtree(root)


# --- quera ---------------------------------------------------------------
@test
def quera_no_cookie_raises_not_authorised():
    root, s = make_vault()
    try:
        q = quera.Quera(s, vaultfs.Vault(s))
        try:
            q.fetch_deadlines()
            ok(False, "must raise without a saved cookie")
        except quera.NotAuthorised:
            pass
    finally:
        shutil.rmtree(root)


@test
def quera_login_page_response_raises_not_authorised():
    import unittest.mock as mock
    root, s = make_vault()
    try:
        q = quera.Quera(s, vaultfs.Vault(s))
        q.save_cookie("sessionid=abc")
        ok(oct(q._cookie_path.stat().st_mode)[-3:] == "600",
           "a session cookie is a standing grant — must not be world-readable")
        resp = mock.Mock(status_code=200, headers={},
                         text='<title>ورود | Quera</title><h1>ورود به کوئرا</h1>')
        with mock.patch("httpx.get", return_value=resp):
            try:
                q.fetch_deadlines()
                ok(False, "a login-page-shaped response must not read as success")
            except quera.NotAuthorised:
                pass
    finally:
        shutil.rmtree(root)


@test
def authenticated_page_furniture_does_not_read_as_a_login_page():
    # Caught against a real logged-in response: every authenticated page
    # carries a hidden change-password field in the account menu and footer
    # links reading "ورود به گیت‌هاب کوئرا" ("go to Quera's GitHub") — both
    # matched the first version of this check and made a real session look
    # expired on every sync.
    ok(not quera._looks_like_login_page(
        '<title>کلاس‌ها | Quera</title>'
        '<input type="password" name="change-password">'
        '<a href="https://github.com/QueraTeam">ورود به گیت‌هاب کوئرا</a>'),
       "password fields and \"ورود به\" footer links appear on real logged-in pages")


@test
def parse_course_list_reads_the_deadline_widget():
    # The real payload keys/shape, taken from a live logged-in response —
    # not guessed prior art the way the old dashboard-HTML parser was.
    blob = {"props": {"pageProps": {"course": {"course_deadline_widget_data": [
        {"id": "42", "name": "HW3", "finish_time": "2026-08-20T23:59:00+00:00",
         "course_name": "Algorithms"},
        {"id": "43", "name": "no due date on this one", "finish_time": None,
         "course_name": "Algorithms"},
    ]}}}}
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(blob) + '</script>')
    out = quera.parse_course_list(html)
    eq(len(out), 1, "an item with no finish_time must be skipped, not crash")
    eq(out[0].title, "HW3")
    eq(out[0].course, "Algorithms")
    eq(out[0].url, "https://quera.org/course/assignments/42/problems")
    # finish_time is UTC; Tehran is UTC+3:30, so 23:59 UTC lands the next
    # calendar day for a student actually reading the site.
    eq(out[0].due_at, dt.datetime(2026, 8, 21, 3, 29))

    eq(quera.parse_course_list('<html>no __NEXT_DATA__ here</html>'), [],
       "a page without the Next.js data blob must yield no deadlines, not an error")
    eq(quera.parse_course_list(
        '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>'),
       [], "a blob missing the widget key must yield no deadlines, not an error")


@test
def generated_assignment_notes_match_the_schema():
    root, s = make_vault()
    try:
        q = quera.Quera(s, vaultfs.Vault(s))
        a = quera.QueraAssignment(course="Algorithms", title="Homework 3",
                                  due_at=dt.datetime(2026, 8, 20),
                                  url="https://quera.org/x")
        rel, meta, body = q._assignment_note(a)
        note = notes.Note(rel=rel, path=Path(rel), meta=meta, body=body)
        errs = [i for i in schema.validate_note(note) if i.level == "error"]
        eq(errs, [], f"template fill produced an invalid note: {errs}")
    finally:
        shutil.rmtree(root)


@test
def quera_sync_creates_then_updates_only_on_drift():
    root, s = make_vault()
    try:
        q = quera.Quera(s, vaultfs.Vault(s))
        a = quera.QueraAssignment(course="Algorithms", title="Homework 3",
                                  due_at=dt.datetime(2026, 8, 20),
                                  url="https://quera.org/x")
        q.fetch_deadlines = lambda: [a]

        report = q.sync(dry_run=False)
        eq(report["created"], ["03 - University/Assignments/Homework 3.md"])
        ok((root / "03 - University/Courses/Algorithms.md").exists(),
           "a course note must be created the first time its name is seen")

        report2 = q.sync(dry_run=False)
        eq(report2["created"], [])
        eq(report2["skipped"], 1, "an unchanged deadline must not rewrite the note")

        a.due_at = dt.datetime(2026, 8, 25)
        report3 = q.sync(dry_run=False)
        eq(len(report3["updated"]), 1)
        eq(report3["updated"][0]["changes"]["due-date"], "2026-08-25")
    finally:
        shutil.rmtree(root)


@test
def category_spend_picks_latest_snapshot_and_only_toman_fields():
    root, s = make_vault()
    try:
        snaps = root / "13 - Finance Tracker/Category Snapshots"
        snaps.mkdir(parents=True)
        (snaps / "1405-01.md").write_text(
            '---\ntype: category-snapshot\njalali-month: "1405-01"\n'
            'gregorian-range: "2026-03-21 to 2026-04-20"\nsource: x\n'
            'complete: true\ndining-toman: 100\nbills-toman: 0\n---\n',
            encoding="utf-8")
        (snaps / "1405-02.md").write_text(
            '---\ntype: category-snapshot\njalali-month: "1405-02"\n'
            'gregorian-range: "2026-04-21 to 2026-05-21"\nsource: x\n'
            'complete: false\ndining-toman: 50\nbills-toman: 200\n'
            'other-field: not-a-number\n---\n',
            encoding="utf-8")
        index.rebuild(s, full=True)
        q = query.QueryEngine(s)

        month, totals = q.category_spend()
        eq(month, "1405-02", "the lexicographically-latest month, unless asked otherwise")
        eq(totals, {"bills": 200.0, "dining": 50.0}, "sorted by amount descending")
        ok("other-field" not in totals, "only -toman keys belong in the totals")

        month2, totals2 = q.category_spend("1405-01")
        eq(month2, "1405-01")
        # zero amounts are dropped — nothing to chart for them
        ok("bills" not in totals2 and totals2 == {"dining": 100.0})

        ok(q.category_spend("1405-99") is None, "no matching snapshot")
        q.close()
    finally:
        shutil.rmtree(root)


@test
def templates_never_count_as_real_notes():
    root, s = make_vault()
    try:
        (root / "05 - Templates").mkdir(parents=True, exist_ok=True)
        (root / "05 - Templates/Issue.md").write_text(
            "---\ntype: issue\nstatus: open\n---\n\ntemplate\n", encoding="utf-8")
        index.rebuild(s, full=True)
        q = query.QueryEngine(s)
        rels = [r.rel for r in q.issues().rows]
        ok(not any("Templates" in r for r in rels), "templates leaked into a query")
        q.close()
    finally:
        shutil.rmtree(root)


# --- router ----------------------------------------------------------
@test
def router_prefers_lookup_over_capture():
    """A misrouted lookup writes a junk note; a misrouted capture only shows results.

    Offline behaviour. With a shell reachable these all go to the agent, which
    has `vault_query`; this is the no-model cascade, where getting the
    lookup/capture call right is the difference between showing results and
    writing a note called *best cafes with wifi*.
    """
    r = router.Router(agentic=False)
    for text in ("best cafes with wifi", "show me my open issues",
                 "which places are open late?", "find the pricing decision"):
        route = r.route(text)
        eq(route.intent, router.QUERY, f"{text!r} should be a lookup")


@test
def router_classifies_captures():
    """Offline: the typed-capture heuristics still sort a capture by topic."""
    r = router.Router(agentic=False)
    cases = {
        "remind me to call mom tomorrow at 9am": router.REMINDER,
        "we decided to use SQLite for reminders": router.CAPTURE_DECISION,
        "bench 80kg 3x5 felt strong": router.CAPTURE_WORKOUT,
        "https://example.com/article": router.CAPTURE_LINK,
    }
    for text, expected in cases.items():
        eq(r.route(text).intent, expected, f"{text!r}")


@test
def router_flags_ambiguity():
    """Offline: a guess about meaning must not present itself as certain."""
    r = router.Router(agentic=False)
    route = r.route("the light was leaving the room")
    ok(route.confidence < 0.9, "vague input must not look certain")


class _StubDecider:
    """A decider that always answers, wrongly, and counts how often it is asked.

    Wrong on purpose: a rule that survives this is one the model was never
    consulted about, which is the property being tested.
    """

    available = True

    def __init__(self, intent=router.CAPTURE_LYRIC):
        self.intent, self.calls = intent, 0

    def decide(self, text):
        from v.core.decide import Decision
        self.calls += 1
        return Decision(self.intent, 0.99, "stub", {})


@test
def certain_rules_never_reach_the_model():
    """The cheap, unambiguous shapes must resolve before a token is spent.

    Each of these is either free and exact or already settled by something the
    user taught us. Paying ~9s and ~$0.01 to have a model re-derive that
    `/status` means status would be the whole cost model inverted.
    """
    stub = _StubDecider()
    r = router.Router(decider=stub, agentic=False)
    for text, expected in (("/status", router.STATUS),
                           ("ISSUE-139", router.QUERY),
                           ("open MRs", router.QUERY),
                           ("hi", router.SMALLTALK),
                           ("help", router.HELP),
                           ("no, that was a lyric", router.REFILE),
                           ("undo", router.UNDO),
                           ("Undo", router.UNDO),
                           ("nevermind", router.UNDO)):
        eq(r.route(text).intent, expected, f"{text!r} should not need a model")
    eq(stub.calls, 0, "a certain rule asked the model anyway")


@test
def undo_is_a_command_not_a_thought():
    """Typing "undo" used to append the word "undo" to the daily note.

    It had no rule outside `/undo`, so it scored nothing and fell all the way
    to capture.note — while the Undo *button* under every write emits this
    exact string. The affordance and the parser disagreed.
    """
    r = router.Router(decider=None, agentic=False)
    for word in ("undo", "Undo", "undo.", "revert", "scratch that", " undo "):
        eq(r.route(word).intent, router.UNDO, f"{word!r} must undo")
    # ...but only when it is the whole message. A sentence about undoing is a
    # sentence, and reverting the user's last write on it would be worse than
    # any misfile.
    for sentence in ("undo culture is exhausting",
                     "i should undo the pricing migration"):
        assert r.route(sentence).intent != router.UNDO, sentence


@test
def the_model_owns_the_content_shaped_call():
    """Anything that isn't a command reaches the model unclassified.

    The heuristics were confidently wrong in a way no confidence threshold
    catches, because the rule was certain: "i should set a fixed time on
    sundays for my 1on1 meetings with Arash" hit CALENDAR_RE on the substring
    `1on1`, scored 0.85, and printed today's calendar. Replacing the regex with
    a classifier moved the problem rather than fixing it — a classifier can
    only pick a label, so "search these vulnerabilities and write them up" got
    `task.add` off the words `i should` and lost the rest of the sentence.

    So the router no longer decides what these mean. It hands them over.
    """
    r = router.Router(decider=None, agentic=True)
    for msg in (
        "i should set a fixed time on sundays for my 1on1 meetings with Arash",
        "so i should learn about those vulnerabilities. do some search for me "
        "and create a simple and compact document so i can read with examples.",
        "no i want you to actually search and document famous vulnerabilities",
        "based on my last 1on1, update my goals and create a todo list",
    ):
        eq(r.route(msg).intent, router.AGENT, f"{msg[:40]!r} needs reading")

    # Offline, the same message falls back to the cascade — wrong, but a wrong
    # answer beats no answer when there is no model to give a right one.
    eq(router.Router(decider=None, agentic=False).route(
        "i should set a fixed time on sundays for my 1on1 meetings with Arash"
    ).intent, router.CALENDAR, "the offline cascade is unchanged")


@test
def commands_never_reach_the_model():
    """The fast paths are whole-message matches, and they stay free.

    This is the boundary the inversion is drawn on: a fixed string cannot turn
    out to have been a request, so routing it locally cannot be wrong. Anything
    that needs a sentence *read* is not on this list.
    """
    stub = _StubDecider()
    r = router.Router(decider=stub, agentic=True)
    for text, expected in (("/status", router.STATUS),
                           ("undo", router.UNDO),
                           ("hi", router.SMALLTALK),
                           ("thanks", router.SMALLTALK),
                           ("help", router.HELP),
                           ("ISSUE-139", router.QUERY),
                           ("/memory", router.RECALL),
                           ("/forget 3", router.TEACH),
                           ("no, that was a lyric", router.REFILE)):
        eq(r.route(text).intent, expected, f"{text!r} should not need a model")
    eq(stub.calls, 0, "a command asked the model anyway")


@test
def a_request_wearing_a_correction_prefix_is_not_a_memory():
    """The bug that put the model in charge of memory.

    "no i want you to actually search and document famous vulnerabilities so i
    can learn them" opened with `no`, which `_CORRECTION_RE` owned, so it was
    filed as a permanent belief — and, because corrections were ALWAYS_ON,
    injected into every subsequent prompt as a standing instruction. It never
    reached a handler. Both of the two memories the store ever held arrived
    this way. There is no longer any phrase that can write to memory.
    """
    from v.core import memory as memory_mod

    for text in ("no i want you to actually search and document famous "
                 "vulnerabilities so i can learn them",
                 "no, they're in 04 - Gym not the daily note",
                 "always call them MRs, not merge requests",
                 "remember that Pricing is the main project"):
        eq(memory_mod.read_command(text), None,
           f"{text[:40]!r} is content, not a memory command")

    eq(memory_mod.read_command("/forget 4"), ("forget", 4))
    eq(memory_mod.read_command("/memory"), ("list", None))
    ok(memory_mod.CORRECTION not in memory_mod.ALWAYS_ON,
       "a correction must earn its place in the prompt by matching the topic")


@test
def a_dead_decider_falls_back_to_the_rules():
    """Every failure path is a fallback, never an error.

    The decider is an optimisation over a system that already worked; if it
    raises, times out, or hedges, the answer must be the one we had before it
    existed rather than a stack trace.
    """
    class Boom:
        available = True

        def decide(self, text):
            raise RuntimeError("no auth, no network, no idea")

    class Hedger:
        available = True

        def decide(self, text):
            from v.core.decide import Decision
            return Decision(router.CAPTURE_LYRIC, 0.01, "unsure", {})

    for bad in (Boom(), Hedger()):
        route = router.Router(decider=bad, agentic=False).route(
            "what are my priorities")
        eq(route.intent, router.TASKS,
           f"{type(bad).__name__} should degrade to the heuristics")


@test
def location_pin_short_circuits():
    route = router.Router().route("nice spot", has_location=True)
    eq(route.intent, router.CAPTURE_PLACE)
    ok(route.confidence > 0.95)


# --- vault writes ----------------------------------------------------
@test
def appends_are_committed_and_undoable():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        rel = "02 - Work/Issues/ISSUE-1 test issue.md"
        change = v.append(rel, "- appended line", heading="Notes")
        ok(change.commit, "a write must produce a git commit")
        ok("appended line" in v.read(rel).body)
        v.undo(change.commit)
        ok("appended line" not in v.read(rel).body, "undo must restore the file")
    finally:
        shutil.rmtree(root)


@test
def hand_written_changes_are_committed_before_v_writes():
    """V commits per path. Append to a note the user had edited but not
    committed and that one `[bot]` commit carries both — so `undo` reverts
    their work along with V's."""
    root, s = make_vault()
    s.git_commit_on_start_exclude = ("assistant", ".state")
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    try:
        v = vaultfs.Vault(s)
        rel = "02 - Work/Issues/ISSUE-1 test issue.md"
        (root / rel).write_text(v.read(rel).path.read_text() + "\nby hand\n",
                                encoding="utf-8")
        eq(v.pending(), [rel], "the hand-written change should be pending")

        c = v.commit_pending()
        ok(c is not None and not v.pending(), "the tree should be clean after")
        subject = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%s"],
            capture_output=True, text=True).stdout
        ok(s.commit_prefix not in subject,
           f"the user's own work must not be filed as V's: {subject!r}")
        ok(not v.history, "and must stay out of undo's reach")

        # V's write now lands on top, alone, and undoes cleanly.
        change = v.append(rel, "- from V", heading="Notes")
        v.undo(change.commit)
        body = v.read(rel).body
        ok("from V" not in body, "undo must take back V's line")
        ok("by hand" in body, "and must leave the hand-written one alone")
    finally:
        shutil.rmtree(root)


@test
def a_clean_tree_produces_no_startup_commit():
    root, s = make_vault()
    s.git_commit_on_start_exclude = ("assistant", ".state")
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    try:
        v = vaultfs.Vault(s)
        eq(v.pending(), [], "the fixture vault starts clean")
        ok(v.commit_pending() is None, "nothing to commit means no commit")
    finally:
        shutil.rmtree(root)


@test
def rewrite_requires_confirmation():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        try:
            v.rewrite("02 - Work/Issues/ISSUE-1 test issue.md", "wiped")
            raise AssertionError("rewrite without confirmation must raise")
        except vaultfs.VaultError:
            pass
    finally:
        shutil.rmtree(root)


@test
def writes_cannot_escape_the_vault():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        try:
            v.append("../../etc/evil.md", "nope")
            raise AssertionError("path traversal must be refused")
        except vaultfs.VaultError:
            pass
    finally:
        shutil.rmtree(root)


@test
def append_lands_under_the_right_heading():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        rel = "note.md"
        v.create(rel, {}, "## One\n\nalpha\n\n## Two\n\nbeta\n")
        v.append(rel, "- added", heading="One")
        body = v.read(rel).body
        one = body.index("## One")
        two = body.index("## Two")
        added = body.index("- added")
        ok(one < added < two, "append must stay inside its own section")
    finally:
        shutil.rmtree(root)


# --- reminders -------------------------------------------------------
@test
def natural_time_parsing():
    now = dt.datetime(2026, 7, 31, 14, 30)
    cases = [
        ("remind me in 2 hours to call", dt.datetime(2026, 7, 31, 16, 30), None),
        ("remind me tomorrow at 9am to review", dt.datetime(2026, 8, 1, 9, 0), None),
        ("every day at 7 do the brief", dt.datetime(2026, 7, 31, 19, 0), "day"),
    ]
    for text, expected, rec in cases:
        due, _, recurrence = reminders.parse_when(text, now)
        eq(due, expected, text)
        eq(recurrence, rec, text)


@test
def reminders_catch_up_after_downtime():
    """The failure the design doc calls the worst a reminder bot can have."""
    root, s = make_vault()
    try:
        r = reminders.Reminders(s)
        past = dt.datetime.now() - dt.timedelta(hours=2)
        r.add("missed while down", past)
        missed = r.catch_up()
        eq(len(missed), 1, "a reminder due during downtime must still fire")
        ok(missed[0].overdue_by > dt.timedelta(hours=1))
    finally:
        shutil.rmtree(root)


@test
def recurring_reminders_reschedule():
    root, s = make_vault()
    try:
        r = reminders.Reminders(s)
        due = dt.datetime.now() - dt.timedelta(minutes=5)
        rem = r.add("daily thing", due, recurrence="day")
        r.mark_fired(rem)
        pending = r.pending()
        eq(len(pending), 1, "a recurring reminder must re-arm itself")
        ok(pending[0].due > dt.datetime.now())
    finally:
        shutil.rmtree(root)


# --- rag -------------------------------------------------------------
@test
def tfidf_backend_needs_no_download():
    b = embed.TfidfBackend()
    b.fit(["pricing discount logic", "gym bench press", "کافه خوب در تهران"])
    v = b.encode(["pricing"])
    eq(v.shape, (1, b.dim))
    import numpy as np
    ok(abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-4, "vectors must be unit length")


@test
def persian_normalisation():
    eq(embed.normalize("كتاب"), embed.normalize("کتاب"))
    eq(embed.normalize("مي‌رود"), embed.normalize("می رود"))


@test
def rag_retrieves_and_cites():
    root, s = make_vault()
    s.embedding_backend = "tfidf"
    try:
        r = rag.Rag(s)
        r.build()
        res = r.search("pricing discounts", k=3)
        ok(res.hits, "expected hits")
        ok(not res.stale, "a freshly built index must not be stale")
        ok(all(h.rel for h in res.hits), "every hit must know its note")
        ok(any("ISSUE-1" in c for c in res.citations()))
    finally:
        shutil.rmtree(root)


# --- capture ---------------------------------------------------------
@test
def capture_creates_schema_valid_notes():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        c = capture.Capture(v, s)
        when = dt.datetime(2026, 7, 31, 10, 0)
        res = c.capture("bench 80kg 3x5",
                        router.Route(router.CAPTURE_WORKOUT, 0.9, "test"), when=when)
        ok(res.ok)
        note = v.read(res.target)
        issues = [i for i in schema.validate_note(note) if i.level == "error"]
        eq(issues, [], f"generated note violates its own schema: {issues}")
    finally:
        shutil.rmtree(root)


@test
def expense_capture_infers_category_and_direction():
    """A spend on a recognisable category lands with category/direction/amount
    inline fields, not the old bare Date|Item|Amount table row."""
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        c = capture.Capture(v, s)
        r = router.Router(agentic=False)
        route = r.route("spent 250k at a restaurant for dinner")
        eq(route.intent, router.CAPTURE_EXPENSE, "should route as an expense")
        when = dt.datetime(2026, 8, 1, 20, 0)
        res = c.capture("spent 250k at a restaurant for dinner", route, when=when)
        ok(res.ok)
        note = v.read(res.target)
        ok("[category:: dining]" in note.body, note.body)
        ok("[direction:: expense]" in note.body, note.body)
        ok("[amount:: 250000]" in note.body, note.body)
        issues = [i for i in schema.validate_note(note) if i.level == "error"]
        eq(issues, [], f"generated note violates its own schema: {issues}")
    finally:
        shutil.rmtree(root)


@test
def income_capture_files_under_income_heading():
    """Getting paid is still a `capture.expense` route, but it must land under
    ## Income with direction:: income, not be miscounted as spending."""
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        c = capture.Capture(v, s)
        r = router.Router(agentic=False)
        route = r.route("got paid 5000k salary today")
        when = dt.datetime(2026, 8, 1, 9, 0)
        res = c.capture("got paid 5000k salary today", route, when=when,
                        confirmed=True)
        ok(res.ok)
        note = v.read(res.target)
        ok(note.section("Income") is not None, "should create an Income section")
        ok("[direction:: income]" in note.body, note.body)
        ok("[amount:: 5000000]" in note.body, note.body)
    finally:
        shutil.rmtree(root)


@test
def low_confidence_files_to_the_daily_note_and_never_asks():
    """A weak guess must not open a prompt, and must not act on the guess.

    Asking first is what made V unanswerable: every message it didn't
    recognise — including "tell me about yourself" — hit a four-button "where
    should this go?" instead of a reply. Filing to today's note is the safe
    version of guessing, because a log line is only unspecific, never wrong.
    """
    root, s = make_vault()
    try:
        c = capture.Capture(vaultfs.Vault(s), s)
        res = c.capture("something vague",
                        router.Route(router.CAPTURE_LYRIC, 0.3, "unsure"))
        ok(res.change is not None, "a weak guess must still be written down")
        eq(res.intent, router.CAPTURE_NOTE, "a weak guess goes to the safe bucket")
        ok(res.target.startswith("01 - Daily Notes"), res.target)
        ok("that was a lyric" in res.message.lower(),
           "the losing guess must be one message away")
    finally:
        shutil.rmtree(root)


@test
def daily_note_scaffold_is_valid():
    root, s = make_vault()
    try:
        c = capture.Capture(vaultfs.Vault(s), s)
        rel = c.ensure_daily(dt.date(2026, 7, 31))
        note = vaultfs.Vault(s).read(rel)
        eq(note.meta["week"], "2026-W31")
        eq(note.meta["month"], "2026-07")
        eq([i for i in schema.validate_note(note) if i.level == "error"], [])
    finally:
        shutil.rmtree(root)


# --- nudges ----------------------------------------------------------
@test
def nudges_are_free_and_survive_failures():
    root, s = make_vault()
    try:
        engine_nudges = nudges.NudgeEngine(s)
        items = engine_nudges.all()
        ok(items, "expected at least one nudge from this fixture")
        eq(sum(n.cost for n in items), 0.0, "nudges must cost nothing")
        ids = {n.id for n in items}
        # `opened` no longer counts as a schema *error* — V normalises it — but
        # it must still surface, because Dataview does not, and a note the
        # dashboards can't see is the whole reason this check exists.
        ok("vault.dashboard-blind" in ids,
           "GitLab's status vocabulary must still raise a nudge")
        ok("crm.overdue" in ids)
    finally:
        shutil.rmtree(root)


# --- engine ----------------------------------------------------------
@test
def engine_never_spends_money():
    root, s = make_vault()
    s.embedding_backend = "tfidf"
    try:
        e = Engine(s)
        e.rag.build()
        for msg in ("/status", "which issues are in-review?", "best cafes",
                    "remind me in 10 minutes to stretch", "what about pricing?"):
            resp = e.handle(msg)
            eq(resp.cost, 0.0, f"{msg!r} cost money")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_widget_can_tick_a_box_it_only_knows_by_id():
    """`complete_task` was emitted as a button by the disambiguation path and
    handled by nothing, so pressing V's own button answered "Unknown action".
    The priorities widget presses it too, and a home screen has no way to say
    what the task was — only its id — so the id has to be enough."""
    root, s = make_vault()
    s.embedding_backend = "tfidf"
    rel = "01 - Daily Notes/2026/07 - July/2026-07-02.md"
    (root / rel).write_text("## Tasks\n\n- [ ] buy milk\n- [ ] ship pricing\n",
                            encoding="utf-8")
    index.rebuild(s, full=True)
    try:
        e = Engine(s)
        task = next(t for t in e.q.tasks.open() if t.text == "buy milk")

        resp = e.act("complete_task", {"id": task.id})
        eq(resp.cost, 0.0, "ticking a box must not reach a model")
        ok("buy milk" in resp.text, resp.text)
        ok("- [x] buy milk" in (root / rel).read_text(encoding="utf-8"))
        ok(any(b.action == "undo" for b in resp.buttons),
           "a write from the home screen still offers the way back")

        # Pressed twice — a widget is a picture, and the stale copy of it that
        # is still on screen after the first tap is pressable.
        again = e.act("complete_task", {"id": task.id})
        ok("already ticked" in again.text, again.text)

        gone = e.act("complete_task", {"id": 1})
        ok("isn't in the index" in gone.text, gone.text)
        e.close()
    finally:
        shutil.rmtree(root)


@test
def null_llm_refuses_loudly():
    provider = llm.NullLLM()
    ok(not provider.available)
    try:
        provider.complete("sys", "user")
        raise AssertionError("NullLLM must raise rather than pretend")
    except llm.LLMUnavailable:
        pass


@test
def privacy_guard_blocks_private_folders():
    s = config.Settings()
    guard = llm.Guard(settings=s)
    guard.check(["02 - Work/Issues/ISSUE-1.md"])       # fine
    try:
        guard.check(["17 - AIT/Sessions/Session 0.md"])
        raise AssertionError("private folder content must not be sent to an API")
    except llm.PrivacyViolation:
        pass


@test
def correction_teaches_the_router():
    root, s = make_vault()
    try:
        r = router.Router(s, agentic=False)
        text = "the light was leaving the room and I stayed"
        r.learn(text, router.CAPTURE_LYRIC)
        again = router.Router(s, agentic=False).route(text)
        eq(again.intent, router.CAPTURE_LYRIC, "a correction must stick")
        ok(again.confidence > 0.7)
    finally:
        shutil.rmtree(root)


@test
def engine_writes_first_and_offers_the_way_back():
    """The write happens now; the correction happens after. Undo is what makes
    that safe — every capture is a `[bot]` commit, so guessing costs nothing
    that a follow-up message can't take back."""
    root, s = make_vault()
    s.embedding_backend = "tfidf"
    try:
        e = Engine(s)
        resp = e.handle("the light was leaving the room")
        ok(resp.data.get("target"), "a capture must write immediately")
        ok(any(b.action == "undo" for b in resp.buttons), "no way back offered")
        eq(e._last_capture["target"], resp.data["target"],
           "the last capture must be remembered, so it can be moved")
        e.close()
    finally:
        shutil.rmtree(root)


# --- scheduler -------------------------------------------------------
@test
def scheduler_catches_up_missed_jobs():
    root, s = make_vault()
    s.embedding_backend = "tfidf"
    try:
        from v.core.scheduler import Scheduler
        e = Engine(s)
        sched = Scheduler(e, s, sink=lambda *a: None)
        missed = sched.catch_up(now=dt.datetime.now().replace(hour=12, minute=0))
        names = [m[0] for m in missed]
        ok(any("morning-brief" in n for n in names),
           "a 7am brief must be caught up when booting at noon")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def quiet_hours_are_respected():
    root, s = make_vault()
    try:
        from v.core.scheduler import Scheduler
        e = Engine(s)
        sched = Scheduler(e, s, sink=lambda *a: None)
        ok(sched._quiet(dt.datetime(2026, 7, 31, 3, 0)), "3am is quiet")
        ok(not sched._quiet(dt.datetime(2026, 7, 31, 10, 0)))
        e.close()
    finally:
        shutil.rmtree(root)


# --- work ------------------------------------------------------------
@test
def gitlab_status_maps_to_vault_vocabulary():
    from v.core.work import STATUS_MAP
    eq(STATUS_MAP["opened"], "open")
    eq(STATUS_MAP["merged"], "merged")


@test
def generated_issue_notes_match_the_schema():
    root, s = make_vault()
    try:
        from v.core.work import Work, GitLabItem
        w = Work(s, vaultfs.Vault(s))
        item = GitLabItem(99, "a title", "open", "Pricing", "2026-07-01",
                          "https://git.divar.cloud/x/-/work_items/99",
                          {"description": "d"})
        rel, meta, body = w.issue_note(item)
        note = notes.Note(rel=rel, path=Path(rel), meta=meta, body=body)
        errs = [i for i in schema.validate_note(note) if i.level == "error"]
        eq(errs, [], f"template fill produced an invalid note: {errs}")
    finally:
        shutil.rmtree(root)


@test
def a_vague_capture_is_filed_not_questioned():
    """Nothing may sit in front of the next message. The ask-vs-guess flow used
    to hold a question open and swallow whatever came next — `/status` and
    "best cafes with wifi" both vanished into it."""
    root, s = make_vault()
    try:
        e = Engine(s)
        r = e.handle("the light was leaving the room")
        ok(r.text.startswith("Logged to today's note"), r.text)
        ok(not r.buttons or all(b.action != "route" for b in r.buttons),
           "a capture must not offer a where-should-this-go choice")
        r = e.handle("best cafes with wifi")
        eq(r.intent, router.QUERY, "an unrelated message was eaten")
        r = e.handle("/status")
        eq(r.intent, router.STATUS, "a command was eaten")
    finally:
        shutil.rmtree(root)


@test
def refiling_moves_the_last_capture_and_teaches_the_router():
    """The correction loop, after the write instead of before it."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.handle("the light was leaving the room")
        r = e.handle("no, that was a lyric")
        eq(r.intent, router.REFILE)
        ok(s.folders.lyrics in (r.data.get("target") or ""),
           f"should have landed in the lyrics folder: {r.data}")
        ok("Noted" in r.text, "a correction should teach the router")
        eq(e.router.route("the light was leaving the room").intent,
           router.CAPTURE_LYRIC, "the correction must stick")
    finally:
        shutil.rmtree(root)


@test
def talking_to_v_is_never_a_capture():
    """"hey V, tell me about yourself and your personality" was a 50%
    capture.note — the exact message the ambiguity prompt made unanswerable."""
    root, s = make_vault()
    try:
        e = Engine(s)
        for line in ("hey V tell me about yourself and your personality",
                     "use claude and talk to me",
                     "V, what do you think about how I've been working"):
            r = e.router.route(line)
            ok(r.intent not in router.CAPTURE_INTENTS,
               f"{line!r} routed to {r.intent}")
        # No shell in the test settings, so chat degrades to what V can do.
        r = e.handle("talk to me")
        eq(r.intent, router.CHAT)
        ok(r.cost == 0.0)
    finally:
        shutil.rmtree(root)


@test
def verbless_lookups_reach_the_query_path():
    """`_structured()` answers these exactly and for free, but the router was
    never sending them there — a working capability made unreachable by the
    phrasing people actually use. "open MRs" was landing in capture.note.

    Offline only: online these reach the agent, which calls `vault_query`
    itself. The rule stays because a shell-less V should still answer them
    exactly rather than guess."""
    r = router.Router(agentic=False)
    for text in ("open MRs", "give me my standup", "somewhere quiet to work",
                 "overdue assignments", "stale issues"):
        eq(r.route(text).intent, router.QUERY, f"{text!r} should be a lookup")


@test
def help_is_recognised_without_a_slash():
    r = router.Router()
    for text in ("help", "?", "what can you do"):
        eq(r.route(text).intent, router.HELP, f"{text!r}")


@test
def met_a_person_needs_a_name():
    """"met Sara" is a CRM note; "met the deadline" must not be. Offline."""
    r = router.Router(agentic=False)
    eq(r.route("met Sara at the conference").intent, router.CAPTURE_PERSON)
    ok(r.route("met the deadline today").intent != router.CAPTURE_PERSON,
       "a lowercase noun must not be mistaken for a person")


@test
def route_records_how_it_was_answered():
    """`handle()` overwrote Response.route with the router's decision, which
    threw away the only signal saying whether an answer was free or not."""
    root, s = make_vault()
    try:
        e = Engine(s)
        r = e.handle("which issues are in-review?")
        ok(r.route.startswith("structured:"),
           f"route should record the retrieval path, got {r.route!r}")
        ok(r.decision.startswith("query"),
           f"decision should record the router, got {r.decision!r}")
    finally:
        shutil.rmtree(root)


@test
def every_phrase_in_the_user_guide_is_answered_for_free():
    """The guide promises these are exact and instant. If a phrasing stops
    reaching `_structured`, it silently degrades to search and the docs lie."""
    root, s = make_vault()
    try:
        e = Engine(s)
        promised = [
            "which issues are in-review?", "what's blocked?",
            "show me stale reviews", "open MRs", "best cafes with wifi",
            "somewhere quiet to work, open late", "when's the next birthday?",
            "who haven't I talked to?", "what assignments are due?",
            "what did I lift last time?", "what was I doing a year ago?",
            "give me my standup",
        ]
        for text in promised:
            r = e.handle(text)
            ok(r.route.startswith("structured:"),
               f"{text!r} degraded to {r.route!r} — the User Guide promises free")
            eq(r.cost, 0.0, f"{text!r} cost money")
    finally:
        shutil.rmtree(root)


@test
def the_brief_leads_with_the_calendar():
    """`daily_calendar.py` had worked since June and nothing called it, so the
    brief shipped without the one thing every design note puts first."""
    root, s = make_vault()
    try:
        e = Engine(s)
        from v.core.agenda import Event

        class FakeAgenda:
            def events(self, day=None, **kw):
                return [Event("09:00–10:00", "standup"),
                        Event("all-day", "conference", all_day=True)]
        e.agenda = FakeAgenda()
        text = e.brief().text
        ok("standup" in text, "the brief must show today's events")
        ok(text.index("standup") < text.index("Worth knowing")
           if "Worth knowing" in text else True,
           "the calendar is the time-critical part — it goes first")
    finally:
        shutil.rmtree(root)


@test
def a_dead_calendar_feed_does_not_break_the_brief():
    """The rest of the brief is local and still worth showing."""
    root, s = make_vault()
    try:
        e = Engine(s)

        class Broken:
            def events(self, day=None, **kw):
                raise OSError("no network")
        e.agenda = Broken()
        text = e.brief().text
        ok(text.strip(), "the brief should survive a dead feed")
    finally:
        shutil.rmtree(root)


@test
def agenda_caches_per_day():
    root, s = make_vault()
    try:
        from v.core.agenda import Agenda, Event
        a = Agenda(s)
        day = dt.date(2026, 8, 1)
        a._write_cache(day, [Event("09:00", "cached thing")])
        got = a.events(day)
        eq(len(got), 1)
        eq(got[0].summary, "cached thing")
        eq(a._read_cache(dt.date(2026, 8, 2)), None, "cache must be per-day")
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------------
# the terminal face


@test
def colour_turns_itself_off_when_nothing_is_watching():
    # The suite's stdout is a pipe, so `ui` must have decided against colour
    # on import. Anything else means `cli ... > file` writes escape codes.
    from v.core import ui
    eq(ui.COLOUR, False, "colour must be off when stdout is not a tty")
    eq("\x1b" in ui.markdown("**bold** and `code`"), False,
       "plain mode must emit no escape sequences")
    eq(ui.markdown("**bold**").strip(), "bold", "markers must be stripped")


@test
def markdown_keeps_every_word_it_was_given():
    from v.core import ui
    src = ("**Today**\n\n· a bullet long enough that it has to wrap at least "
           "once across the line, twice if the terminal is narrow\n"
           "· another one\n\n> quoted\n\n```\ncode  spaced\n```")
    out = ui.markdown(src, cols=40)
    for word in ("Today", "bullet", "narrow", "another", "quoted", "spaced"):
        ok(word in out, f"wrapping lost {word!r}")
    ok(all(len(line) <= 60 for line in out.splitlines()),
       "prose was not wrapped")


@test
def width_is_measured_in_columns_not_characters():
    from v.core import ui
    # ZWNJ is the نیم‌فاصله this vault is full of: a character that occupies
    # no column. Counting it is what made every Persian table ragged.
    eq(ui.cell_width("نیم‌فاصله"), len("نیمفاصله"))
    eq(ui.cell_width("abc"), 3)
    eq(ui.cell_width("你好"), 4, "CJK is two columns per character")
    eq(ui.cell_width(ui.pad("ab", 6)), 6)
    eq(ui.truncate("abcdef", 4), "abc…")
    eq(ui.truncate("abc", 9), "abc")


@test
def tables_are_built_to_fit_the_terminal():
    root, s = make_vault()
    try:
        q = query.QueryEngine(s)
        res = q.issues()
        ok(res.rows, "fixture has no issues to table")
        for avail in (40, 72, 120):
            table = res.table(limit=5, cols_available=avail)
            widest = max(ui_width(line) for line in table.splitlines())
            ok(widest <= avail,
               f"table is {widest} columns wide in a {avail}-column terminal")
    finally:
        shutil.rmtree(root)


def ui_width(line: str) -> int:
    from v.core import ui
    return ui.cell_width(line)


# ---- sessions --------------------------------------------------------
@test
def a_session_records_both_halves_and_files_itself_on_close():
    """The old log kept what you asked and threw away what V said, so there was
    nothing to write a recap *from*. And the recap must cost nothing: no model
    is reachable here (BRAIN_NO_MODEL), which is exactly when you most want it.
    """
    root, s = make_vault()
    try:
        e = Engine(s)
        e.start_session(now=dt.datetime(2026, 8, 1, 9, 0))
        e.handle("hello", now=dt.datetime(2026, 8, 1, 9, 0))
        e.handle("who are you", now=dt.datetime(2026, 8, 1, 9, 1))

        recap = e.end_session("quit", now=dt.datetime(2026, 8, 1, 9, 5))
        ok(recap is not None, "two turns should produce a recap")
        eq(recap.n, 2)
        ok(recap.filed, "the recap should have reached the vault")

        log = (root / "19 - Assistant/V/Logs/2026-08-01.md").read_text(
            encoding="utf-8")
        ok("## Sessions" in log, "the recap goes under its own heading")
        ok("hello" in log and "who are you" in log, "what you asked")
        ok("**V**" in log, "and what V answered — the half that used to be lost")
        ok("09:00 – 09:05" in log, "the window it covers")

        # The transcript exists independently of the vault write.
        ok(recap.path.exists(), "raw transcript should survive on disk")

        eq(e.end_session("quit", now=dt.datetime(2026, 8, 1, 9, 6)), None,
           "closing twice must not file a second copy")
    finally:
        shutil.rmtree(root)


@test
def a_new_session_does_not_inherit_the_previous_thread():
    """`hermes._sticky` suspends the cold-thread expiry while *a* session is
    open, not while the session that opened the thread is. Without a reset at
    `start_session`, a thread nothing ever closed — a Telegram message, a
    crashed repl — is adopted by the next session however old it is, and the
    real one grew to 240K tokens across two days and two surfaces before the
    persona washed out of it.
    """
    root, s = make_vault()
    try:
        e = Engine(s)
        # A thread left behind by something that never ended cleanly.
        e.hermes._save({"session_id": "39abbf9e",
                        "last_used": "2026-09-01T20:28:42"})

        e.start_session(now=dt.datetime(2026, 9, 2, 13, 46))
        eq(e.hermes.current_session(dt.datetime(2026, 9, 2, 13, 46)), None,
           "a new session starts a new thread, not yesterday's")
    finally:
        shutil.rmtree(root)


@test
def an_empty_session_writes_nothing():
    """Starting V and quitting without saying anything is not an event, and a
    log full of empty stubs is a log nobody opens."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.start_session()
        eq(e.end_session("quit"), None)
        ok(not (root / "19 - Assistant/V/Logs").exists(),
           "nothing said, nothing written")
    finally:
        shutil.rmtree(root)


@test
def a_session_a_crash_left_open_is_filed_by_the_next_run():
    """A kill -9 never reaches the exit path. The transcript is already on disk
    though, so the next run adopts it — under the day it happened, not today."""
    root, s = make_vault()
    try:
        from v.core.session import SessionStore
        e = Engine(s)
        e.start_session(now=dt.datetime(2026, 7, 30, 22, 0))
        e.handle("something from that night", now=dt.datetime(2026, 7, 30, 22, 1))

        # Simulate the process dying: the session stays marked open, and the
        # next run is a different pid.
        store = SessionStore(s)
        raw = json.loads((s.state_dir / "session.json").read_text())
        raw["pid"] = 999999
        (s.state_dir / "session.json").write_text(json.dumps(raw))

        filed = Engine(s).start_session(now=dt.datetime(2026, 8, 1, 9, 0))
        eq(len(filed), 1, "the orphan should be adopted")
        eq(filed[0].reason, "interrupted")
        ok((root / "19 - Assistant/V/Logs/2026-07-30.md").exists(),
           "it belongs to the day it happened, not the day it was found")
        ok(store.current() is not None, "and a new session is now open")
    finally:
        shutil.rmtree(root)


@test
def a_recap_quotes_bodies_so_they_cannot_forge_headings():
    """A `#` in something you typed must not become a heading in the daily
    log — that would restructure the note V is appending to."""
    from v.core.session import Session, Turn, render
    sess = Session(id="x", started="2026-08-01T09:00:00", pid=1)
    turns = [Turn(at="2026-08-01T09:00:00", text="# not a heading",
                  answer="- not a bullet\n\nsecond para", route="rag:tfidf")]
    out = render(sess, turns, ended=dt.datetime(2026, 8, 1, 9, 2))
    for line in out.splitlines():
        ok(not line.startswith("#") or line.startswith("###"),
           f"unquoted markup leaked into the log: {line!r}")
    ok("> # not a heading" in out)
    ok("> - not a bullet" in out)


# --- render (charts/maps) ---------------------------------------------
def _self_contained(page: str) -> None:
    """No CDN, no tile server, no network — the whole point of building
    these as one file rather than a hosted page."""
    ok("<script src=" not in page, "external script")
    ok("<link " not in page, "external stylesheet")
    ok("http://" not in page and "https://" not in page, "external request")
    ok(page.strip().startswith("<!doctype html>"), "must be a full document")


@test
def bar_chart_is_self_contained_and_carries_the_data():
    page = render.chart_bar_html(["dining", "bills"], [500000, 200000],
                                 title="Spend", unit="toman")
    _self_contained(page)
    ok("dining" in page and "bills" in page)
    ok("Spend" in page)


@test
def line_chart_is_self_contained_and_carries_the_data():
    page = render.chart_line_html([("08-01", 78.4), ("08-02", 77.9)],
                                  title="Weight", unit="kg")
    _self_contained(page)
    ok("08-01" in page and "78.4" in page)


@test
def map_is_self_contained_and_carries_the_points():
    page = render.map_html(
        [{"label": "Cafe A", "lat": 35.7, "lng": 51.4, "category": "cafe",
          "rating": 9}],
        title="Places")
    _self_contained(page)
    ok("Cafe A" in page)


@test
def chart_functions_reject_empty_input():
    for fn, args in ((render.chart_bar_html, ([], [])),
                     (render.chart_line_html, ([],)),
                     (render.map_html, ([],))):
        try:
            fn(*args)
            ok(False, f"{fn.__name__} should refuse empty input")
        except ValueError:
            pass


@test
def save_never_opens_a_browser():
    """`save()` and `open_file()` are split precisely so a test — or any
    other caller that just wants the file — never launches one."""
    import unittest.mock as mock
    root, s = make_vault()
    try:
        with mock.patch("webbrowser.open") as opened:
            path = render.save(render.chart_bar_html(["a"], [1]),
                               slug="t", settings=s)
            ok(path.exists() and path.suffix == ".html")
            ok(path.parent == s.state_dir / "renders")
            opened.assert_not_called()
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------------
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
