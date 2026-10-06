"""Tests for the Navasan exchange-rate board (`core/rates.py`).

Run:  python3 -m v.tests.test_rates

No network, ever: `httpx.get` is replaced in every test that could reach it,
and the fake counts its calls — because the claim this module exists to make
is about *how many requests it sends*, and a count is the only proof of that.

The claims, each one easy to break by accident later:

* **One request a day, however many readers.** The HUD, the 10:00 job and a
  tool call on the same morning cost one request between them.
* **A refresh can never eat tomorrow's daily.** Refreshes stop at
  `MANUAL_BUDGET`; the daily keeps running after they do.
* **The cap is a wall.** At `CAP` requests in the window nothing is sent, by
  either path.
* **The key is never repeated.** Not in an error, not in the state file —
  Navasan takes it in the query string, which is exactly where an exception
  would print it from.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
import traceback
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from v.core import config, rates

PASS, FAIL = [], []
KEY = "free-TESTKEY-0123456789"


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


BODY = {
    "usd_sell": {"value": "104500", "change": 500, "timestamp": 1791270000,
                 "date": "1405-07-14 12:00:00"},
    "eur": {"value": "121,300", "change": -300, "timestamp": 1791270000,
            "date": "1405-07-14 12:00:00"},
    "18ayar": {"value": "9150000", "change": 0, "timestamp": 1791270000,
               "date": "1405-07-14 12:00:00"},
    # Coins arrive in thousands of toman — this is the live board's shape.
    "sekkeh": {"value": "274500", "change": 1500,
               "timestamp": 1791270000, "date": "1405-07-14 12:00:00"},
    "bahar": {"value": "262000", "change": None, "timestamp": 1791270000,
              "date": "1405-07-14 12:00:00"},
    "usd_xau": {"value": "2650.5", "change": 4.5, "timestamp": 1791270000,
                "date": "1405-07-14 12:00:00"},
    "xau": {"value": "1112856370", "change": 0, "timestamp": 1791270000,
            "date": "1405-07-14 12:00:00"},
    "broken": {"value": "", "change": 1},
    "not_a_row": 7,
}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = BODY if body is None else body
        self.text = text or json.dumps(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Fake:
    """`httpx.get`, answering from a script and counting what it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers) or [Resp()]
        self.calls = []

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def fresh(key: str = KEY):
    root = Path(tempfile.mkdtemp(prefix="v-rates-test-"))
    s = config.Settings(vault=root, state_dir=root / "state")
    s.navasan_api_key = key
    s.state_dir.mkdir(parents=True)
    return root, rates.Rates(s)


def at(day: int, hour: int = 9, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute)


def seed(r: rates.Rates, rows: list[tuple[dt.datetime, str]]) -> None:
    """Pretend these requests were already made."""
    state = r._load()
    state["spent"] = [{"at": t.isoformat(timespec="seconds"), "kind": k,
                       "counted": True, "ok": True} for t, k in rows]
    r._save(state)


# --- parsing ---------------------------------------------------------------
@test
def a_board_reads_navasan_strings_and_drops_what_it_cannot():
    q = rates.parse(BODY)
    eq(q["usd_sell"].value, 104500.0)
    eq(q["eur"].value, 121300.0, "a thousands comma must not break a value")
    ok("broken" not in q and "not_a_row" not in q, "unreadable rows dropped")
    eq(q["bahar"].change, None, "a missing change is None, not 0")
    eq(q["usd_sell"].unit, "toman")
    eq(q["usd_xau"].unit, "USD", "usd_xau is the one symbol in dollars")
    eq(q["xau"].unit, "toman", "xau is the same ounce, in toman")
    eq(q["usd_sell"].pct, round(100 * 500 / 104000, 2),
       "the percentage is against yesterday's value, not today's")
    eq(q["usd_sell"].jdate, "1405-07-14 12:00:00")


@test
def coins_are_rescaled_out_of_thousands():
    """Checked on the live board, 2026-10-06: `sekkeh` said 274,500 on a day
    the coin's gold alone was worth ~264,000,000 T. Read as toman, the HUD
    put a coin a thousand times under an 18k gram — this is the fix."""
    q = rates.parse(BODY)
    eq(q["sekkeh"].value, 274_500_000.0)
    eq(q["sekkeh"].change, 1_500_000.0, "the change is rescaled with it")
    eq(q["sekkeh"].pct, rates.parse({"x": {"value": "274500",
                                            "change": 1500}})["x"].pct,
       "and the percentage does not move")
    eq(q["bahar"].value, 262_000_000.0)
    eq(q["bahar"].change, None, "a missing change stays missing")
    ok(q["sekkeh"].value > q["18ayar"].value, "a coin outweighs a gram")
    eq(rates.format_value(q["sekkeh"]), "274,500,000 T")


@test
def a_body_with_no_prices_is_an_error_not_an_empty_board():
    for body in ({}, {"error": "invalid api key"}, [], "nope"):
        try:
            rates.parse(body)
        except rates.RatesError:
            continue
        raise AssertionError(f"{body!r} parsed as a board")


@test
def the_words_carry_the_unit_and_the_direction():
    q = rates.parse(BODY)
    eq(rates.format_value(q["usd_sell"]), "104,500 T")
    eq(rates.format_value(q["usd_xau"]), "$2,650.50")
    ok(rates.format_change(q["usd_sell"]).startswith("▲ 500 T"))
    ok(rates.format_change(q["eur"]).startswith("▼ 300 T"))
    eq(rates.format_change(q["18ayar"]), "unchanged since yesterday")


# --- one a day ---------------------------------------------------------------
@test
def every_reader_of_a_day_shares_one_request():
    root, r = fresh()
    try:
        fake = Fake()
        with mock.patch("httpx.get", fake):
            b1 = r.ensure_today(at(6, 8))
            b2 = r.ensure_today(at(6, 10))
            b3 = rates.Rates(r.s).ensure_today(at(6, 22))   # another process
        eq(len(fake.calls), 1, "three readers, one request")
        ok(b1 and b2 and b3)
        eq(b3.get("usd_sell").value, 104500.0)
        eq(fake.calls[0][1].get("api_key"), KEY)
        ok(not r.due(at(6, 23)), "nothing owed for the rest of the day")
        ok(r.due(at(7, 0, 5)), "and a new day is owed one")
        with mock.patch("httpx.get", fake):
            r.ensure_today(at(7, 9))
        eq(len(fake.calls), 2)
        b = r.budget(at(7, 9))
        eq((b.used, b.manual), (2, 0))
    finally:
        shutil.rmtree(root)


@test
def nothing_is_sent_without_a_key():
    root, r = fresh(key="")
    try:
        fake = Fake()
        with mock.patch("httpx.get", fake):
            eq(r.ensure_today(at(6)), None)
            try:
                r.refresh(at(6))
                raise AssertionError("a refresh with no key went through")
            except rates.NotConfigured:
                pass
        eq(fake.calls, [])
    finally:
        shutil.rmtree(root)


# --- refreshes ---------------------------------------------------------------
@test
def the_first_click_of_a_day_is_the_days_request():
    """A ↻ at 08:00 on a day with no board fetches the day's board — and the
    10:00 job then has nothing to do. One request, not two."""
    root, r = fresh()
    try:
        fake = Fake()
        with mock.patch("httpx.get", fake):
            b = r.refresh(at(6, 8))
            eq(b.kind, "daily")
            r.ensure_today(at(6, 10))
        eq(len(fake.calls), 1)
        eq(r.budget(at(6, 10)).manual, 0, "it did not cost a refresh")
    finally:
        shutil.rmtree(root)


@test
def a_refresh_inside_the_cooldown_is_refused_with_a_time():
    root, r = fresh()
    try:
        fake = Fake()
        with mock.patch("httpx.get", fake):
            r.ensure_today(at(6, 9))
            try:
                r.refresh(at(6, 9, 10))
                raise AssertionError("refreshed ten minutes after a fetch")
            except rates.Refused as exc:
                ok("09:30" in str(exc), f"say when it opens: {exc}")
            b = r.refresh(at(6, 9, 31))
        eq(b.kind, "refresh")
        eq(len(fake.calls), 2)
        eq(r.budget(at(6, 10)).manual, 1)
    finally:
        shutil.rmtree(root)


@test
def the_agents_brake_returns_a_young_board_without_asking():
    root, r = fresh()
    try:
        fake = Fake()
        with mock.patch("httpx.get", fake):
            r.ensure_today(at(6, 9))
            b = r.refresh(at(6, 10), min_age=rates.FREE_TIER_LAG)
            eq(b.fetched, at(6, 9), "an hour-old board is as fresh as it gets")
            b = r.refresh(at(6, 11, 30), min_age=rates.FREE_TIER_LAG)
            eq(b.fetched, at(6, 11, 30))
        eq(len(fake.calls), 2)
    finally:
        shutil.rmtree(root)


@test
def refreshes_run_out_before_the_daily_does():
    root, r = fresh()
    try:
        now = at(20, 12)
        spent = [(now - dt.timedelta(hours=1 + i), "refresh")
                 for i in range(rates.MANUAL_BUDGET)]
        seed(r, spent)
        fake = Fake()
        with mock.patch("httpx.get", fake):
            r.ensure_today(now)
            eq(len(fake.calls), 1, "the daily still runs with no refreshes left")
            try:
                r.refresh(now + dt.timedelta(hours=1))
                raise AssertionError("a refresh past the manual budget")
            except rates.Refused as exc:
                ok("refreshes" in str(exc), str(exc))
        eq(len(fake.calls), 1)
        eq(r.budget(now + dt.timedelta(hours=1)).refreshes_left, 0)
    finally:
        shutil.rmtree(root)


@test
def at_the_cap_nothing_is_sent_by_either_path():
    root, r = fresh()
    try:
        now = at(25, 12)
        seed(r, [(now - dt.timedelta(hours=1 + i), "daily")
                 for i in range(rates.CAP)])
        fake = Fake()
        with mock.patch("httpx.get", fake):
            eq(r.ensure_today(now), None)
            try:
                r.refresh(now)
                raise AssertionError("a request past the cap")
            except rates.Refused as exc:
                ok(str(rates.CAP) in str(exc), str(exc))
        eq(fake.calls, [])
        ok(rates.CAP < rates.QUOTA, "the cap sits under Navasan's quota")
        ok(rates.MANUAL_BUDGET + rates.WINDOW_DAYS <= rates.CAP,
           "a month of dailies plus every refresh fits under the cap")
    finally:
        shutil.rmtree(root)


@test
def the_window_rolls_rather_than_resetting():
    root, r = fresh()
    try:
        now = at(25, 12)
        old = now - dt.timedelta(days=rates.WINDOW_DAYS, hours=1)
        seed(r, [(old, "refresh")] * 50 + [(now - dt.timedelta(days=3), "refresh")])
        b = r.budget(now)
        eq((b.used, b.manual), (1, 1), "requests older than the window drop out")
    finally:
        shutil.rmtree(root)


# --- failures ----------------------------------------------------------------
@test
def a_request_that_never_left_is_not_counted():
    root, r = fresh()
    try:
        fake = Fake(httpx.ConnectError("no route"))
        with mock.patch("httpx.get", fake):
            eq(r.ensure_today(at(6, 9)), None)
        eq(r.budget(at(6, 9)).used, 0)
        ok("unreachable" in r.failure()["why"])
        ok(not r.due(at(6, 9, 30)), "but it is not retried on every poll")
        ok(r.due(at(6, 10, 1)), "and it is retried after an hour")
    finally:
        shutil.rmtree(root)


@test
def automatic_fetches_give_up_for_the_day():
    root, r = fresh()
    try:
        fake = Fake(Resp(500, text="boom"))
        with mock.patch("httpx.get", fake):
            for h in range(8, 20):
                r.ensure_today(at(6, h))
        eq(len(fake.calls), rates.AUTO_TRIES,
           "a dead service costs AUTO_TRIES a day, not one per hour")
        eq(r.budget(at(6, 20)).used, rates.AUTO_TRIES, "and a 500 still counts")
        ok("500" in r.failure()["why"])
    finally:
        shutil.rmtree(root)


@test
def a_refused_key_stops_the_day_at_once():
    root, r = fresh()
    try:
        fake = Fake(Resp(401, body={"error": "bad key"}))
        with mock.patch("httpx.get", fake):
            r.ensure_today(at(6, 8))
            r.ensure_today(at(6, 12))
        eq(len(fake.calls), 1)
        ok("key" in r.failure()["why"])
    finally:
        shutil.rmtree(root)


@test
def a_failed_fetch_keeps_yesterdays_board():
    root, r = fresh()
    try:
        with mock.patch("httpx.get", Fake()):
            r.ensure_today(at(5, 9))
        with mock.patch("httpx.get", Fake(Resp(503, text="down"))):
            b = r.ensure_today(at(6, 9))
        eq(b.fetched, at(5, 9), "the old board is still the answer")
        ok(r.failure(), "and the failure is on record to say why")
    finally:
        shutil.rmtree(root)


@test
def the_key_is_never_repeated():
    root, r = fresh()
    try:
        echo = f"bad request for https://api.navasan.tech/latest/?api_key={KEY}"
        with mock.patch("httpx.get", Fake(Resp(400, body={}, text=echo))):
            try:
                r.refresh(at(6, 9))
            except rates.RatesError as exc:
                ok(KEY not in str(exc), f"the key leaked into the error: {exc}")
        with mock.patch("httpx.get",
                        Fake(httpx.ReadTimeout(f"timed out: ?api_key={KEY}"))):
            try:
                r.refresh(at(6, 10))
            except rates.RatesError as exc:
                ok(KEY not in str(exc), f"the key leaked from httpx: {exc}")
        stored = r.path.read_text(encoding="utf-8")
        ok(KEY not in stored, "the key reached the state file")
        eq(r.budget(at(6, 11)).used, 2, "both reached the server and count")
    finally:
        shutil.rmtree(root)


# --- history -----------------------------------------------------------------
@test
def history_keeps_one_point_per_day_and_leaves_gaps():
    root, r = fresh()
    try:
        with mock.patch("httpx.get", Fake()):
            r.ensure_today(at(1, 9))
            r.ensure_today(at(4, 9))
            r.refresh(at(4, 12))
        h = r.history(30, today=dt.date(2026, 10, 4))
        eq([d for d, _ in h["usd_sell"]], ["2026-10-01", "2026-10-04"],
           "a day nobody fetched is a gap, not a point")
        eq(set(h), {k for k, *_ in rates.TRACKED})
        eq(r.history(2, today=dt.date(2026, 10, 4))["eur"],
           [("2026-10-04", 121300.0)])
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
