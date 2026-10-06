"""What the toman is worth today — dollar, euro, gold — from Navasan.

`bazaar` answers what a *thing* costs. This answers the question under every
one of those: what the money itself is worth this morning. Nothing in the
vault knows, and nothing should be quoted from memory — the free-market dollar
moves by the day and a remembered rate reads exactly as confident as a
checked one.

**The source is Navasan (navasan.tech), and the whole design is its quota.**
The free plan is 120 requests a month, refreshed by them every two hours. One
`GET /latest/?api_key=…` returns *every* symbol they carry — a hundred-odd
currencies, coins and gold — so there is never a reason to ask twice for two
items: one answer is stored whole on disk and every reader is served from it.
The tool, the HUD card and the scheduler are three processes and they share
one file, under one lock, so a question asked in all three at 08:01 costs one
request rather than three.

How the 120 are spent, and why the arithmetic always closes:

* **One a day, automatically.** The first reader of a Tehran day — the HUD
  opening, a question on Telegram, the `rates` job at 10:00 — fetches; every
  later reader that day gets the stored board. That is ≤31 a month.
* **The rest are refreshes**, which only a person asks for (the ↻ on the HUD
  card, or Parsa saying "the price *now*"). They come out of `MANUAL_BUDGET`,
  which is `CAP` minus a month of dailies, so a busy week of clicking can
  never eat the request tomorrow morning needs.
* **`CAP` is 110, not 120.** The ledger here only sees requests V made; a
  `curl` to test the key, or the same key used anywhere else, is invisible to
  it. Ten requests of slack is what makes "never hits the wall" true without
  having to know about those.
* **The window rolls over 31 days** rather than resetting on the 1st, because
  Navasan does not say when *its* month starts. A rolling count under the cap
  is under the cap for any month that could be meant — Gregorian, Jalali, or
  thirty days from sign-up.
* A refresh within `COOLDOWN` of the last request is refused outright: the
  free plan's numbers move every two hours, so a second click a minute later
  could only ever spend a request to be told the same thing.

**What counts as spent** is every request that reached Navasan, answered or
not — a 500 still came off the quota as far as anyone here can tell. A request
that never left the machine (DNS, no route) is recorded and *not* counted.
Automatic fetches give up for the day after `AUTO_TRIES` attempts that
reached the server; a bad key or an exhausted quota stops them at once,
because retrying either one only spends more of what is already gone.

**Units.** Navasan quotes the Iranian market in **toman** — the vault's unit,
so these drop into a ledger comparison as they are. The global ones it also
carries (an ounce of gold, the crypto coins) are dollars; `UNITS` marks the
ones known to be, and anything else is reported as Navasan gave it.

**The key never leaves this module.** It goes in the query string, which is
exactly where `httpx` exceptions print it from, so no exception text from the
request is ever passed on — every failure is reworded here first.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings

LATEST = "https://api.navasan.tech/latest/"

#: Navasan's free plan, and V's own ceiling under it. See the module docstring
#: for why the two differ.
QUOTA = 120
CAP = 110
WINDOW_DAYS = 31
#: What refreshes may spend in any window: the cap less a month of dailies.
MANUAL_BUDGET = CAP - WINDOW_DAYS
#: The shortest gap between two requests a person can cause.
COOLDOWN = dt.timedelta(minutes=30)
#: How often the free plan's numbers actually move. A board younger than this
#: is as fresh as the plan gets, so V's own tool will not refresh it.
FREE_TIER_LAG = dt.timedelta(hours=2)
#: Automatic attempts per day that reached the server, and the wait between.
AUTO_TRIES = 2
AUTO_RETRY = dt.timedelta(hours=1)
#: Days of the tracked items kept for the HUD's sparkline and "a week ago".
HISTORY_DAYS = 120
#: How long the request ledger is kept. Longer than the window, so the count
#: is never short.
LEDGER_DAYS = 40
TIMEOUT = 12.0

#: The four the HUD card shows and the history keeps, in that order.
TRACKED: tuple[tuple[str, str, str], ...] = (
    ("usd_sell", "Dollar", "دلار"),
    ("eur", "Euro", "یورو"),
    # Per gram. Said in the Persian, which is Navasan's own label, because the
    # English column on the HUD card has room for "Gold 18k" and no more.
    ("18ayar", "Gold 18k", "گرم طلای ۱۸ عیار"),
    ("sekkeh", "Emami coin", "سکه امامی"),
)

#: Names for the rest of what people actually ask about, from Navasan's own
#: symbol table. Anything not here is still answerable — it is reported under
#: Navasan's key rather than a name.
NAMES: dict[str, tuple[str, str]] = {
    **{k: (en, fa) for k, en, fa in TRACKED},
    "usd_buy": ("Dollar (buy)", "دلار خرید"),
    "bahar": ("Bahar Azadi coin", "سکه بهار آزادی"),
    "nim": ("Half coin", "نیم سکه"),
    "rob": ("Quarter coin", "ربع سکه"),
    "gerami": ("Gerami coin", "سکه گرمی"),
    "abshodeh": ("Melted gold · 1 mesghal", "مثقال طلای آبشده"),
    "dirham_dubai": ("Dubai dirham", "درهم دبی"),
    "aed": ("UAE dirham", "درهم امارات"),
    "gbp": ("Pound", "پوند"),
    "try": ("Turkish lira", "لیر ترکیه"),
    "cny": ("Yuan", "یوان"),
    "usdt": ("Tether", "تتر"),
    "usd_farda_sell": ("Dollar (forward)", "دلار فردایی"),
    "harat_naghdi_sell": ("Herat dollar", "دلار هرات"),
}

#: Quoted in dollars, not toman. Everything else on the board is the Iranian
#: market and is toman.
USD_QUOTED = frozenset({"usd_xau", "xau", "xag", "btc", "eth", "ltc", "xrp",
                        "eos", "dash", "bnb", "ada", "doge", "sol", "trx"})

_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")


class RatesError(RuntimeError):
    """Navasan did not answer usefully, or V would not ask it. Either way the
    message is safe to show — it never carries the key."""


class NotConfigured(RatesError):
    pass


class Refused(RatesError):
    """V's own budget said no. Not a failure of the service."""


@dataclass
class Quote:
    key: str
    value: float
    change: float | None = None
    updated: dt.datetime | None = None     # when Navasan last moved it
    jdate: str = ""                        # the same instant, as Navasan wrote it

    @property
    def name(self) -> str:
        return NAMES.get(self.key, (self.key, ""))[0]

    @property
    def fa(self) -> str:
        return NAMES.get(self.key, ("", ""))[1]

    @property
    def unit(self) -> str:
        return "USD" if self.key in USD_QUOTED else "toman"

    @property
    def pct(self) -> float | None:
        """Day-over-day, as a percentage of yesterday's value. Navasan gives
        the change as an amount; the percentage is what reads at a glance."""
        if self.change is None:
            return None
        before = self.value - self.change
        return round(100.0 * self.change / before, 2) if before else None

    def json(self) -> dict:
        return {"key": self.key, "name": self.name, "fa": self.fa,
                "unit": self.unit, "value": self.value,
                "change": self.change, "pct": self.pct,
                "updated": (self.updated.isoformat(timespec="minutes")
                            if self.updated else ""),
                "jdate": self.jdate}


@dataclass
class Board:
    """One answer from Navasan: every symbol, and when V fetched it."""
    fetched: dt.datetime
    quotes: dict[str, Quote] = field(default_factory=dict)
    kind: str = "daily"                    # daily | refresh

    def get(self, key: str) -> Quote | None:
        return self.quotes.get(key)

    @property
    def updated(self) -> dt.datetime | None:
        """The newest of the tracked quotes' own timestamps — how old the
        *numbers* are, which on the free plan is not how old the fetch is."""
        times = [q.updated for k, *_ in TRACKED
                 if (q := self.quotes.get(k)) and q.updated]
        return max(times) if times else None


@dataclass
class Budget:
    used: int                 # requests that reached Navasan, this window
    manual: int               # of those, refreshes
    cap: int = CAP
    manual_cap: int = MANUAL_BUDGET
    fetched_today: bool = False
    next_refresh: dt.datetime | None = None   # None: allowed now
    why_not: str = ""

    @property
    def left(self) -> int:
        return max(0, self.cap - self.used)

    @property
    def refreshes_left(self) -> int:
        return max(0, min(self.manual_cap - self.manual, self.left))

    def json(self) -> dict:
        return {"used": self.used, "cap": self.cap, "quota": QUOTA,
                "left": self.left, "manual": self.manual,
                "manual_cap": self.manual_cap,
                "refreshes_left": self.refreshes_left,
                "window_days": WINDOW_DAYS,
                "fetched_today": self.fetched_today,
                "can_refresh": self.next_refresh is None and not self.why_not,
                "next_refresh": (self.next_refresh.isoformat(timespec="minutes")
                                 if self.next_refresh else ""),
                "why_not": self.why_not}


# --------------------------------------------------------------------------
# parsing — a field that stops existing costs that field, not the call


def _number(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "").replace("٬", "")
    try:
        return float(text)
    except ValueError:
        return None


def _when(raw) -> dt.datetime | None:
    n = _number(raw)
    if not n or n <= 0:
        return None
    try:
        # Local, naive — the process is pinned to Asia/Tehran, which is the
        # zone every other timestamp in V is written in.
        return dt.datetime.fromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return None


def parse(payload) -> dict[str, Quote]:
    """Navasan's body -> quotes. A symbol with no readable value is dropped;
    a body with no readable symbols at all is an error, because an empty
    board stored as today's would hide the failure until tomorrow."""
    if not isinstance(payload, dict):
        raise RatesError("Navasan answered with something that is not a "
                         "price list — the API may have changed shape")
    out: dict[str, Quote] = {}
    for key, row in payload.items():
        if not isinstance(row, dict) or not _KEY_RE.match(str(key)):
            continue
        value = _number(row.get("value"))
        if value is None or value <= 0:
            continue
        out[key] = Quote(key=key, value=value,
                         change=_number(row.get("change")),
                         updated=_when(row.get("timestamp")),
                         jdate=str(row.get("date") or ""))
    if not out:
        # The shape a refusal comes back in is undocumented; whatever it said
        # is worth passing on, minus anything that could be the key.
        hint = _scrub(json.dumps(payload, ensure_ascii=False)[:160])
        raise RatesError(f"Navasan's answer had no prices in it: {hint}")
    return out


def _scrub(text: str, key: str = "") -> str:
    if key:
        text = text.replace(key, "…")
    return re.sub(r"api_key=[^&\s\"']+", "api_key=…", text)


# --------------------------------------------------------------------------
# the store


class Rates:
    """The board, the ledger and the lock. Cheap to build — no I/O until a
    method is called — so every caller can make its own."""

    def __init__(self, s: Settings | None = None):
        self.s = s or default_settings
        self.path = self.s.state_dir / "rates.json"
        self.lock_path = self.s.state_dir / "rates.lock"

    @property
    def key(self) -> str:
        return (getattr(self.s, "navasan_api_key", "") or "").strip()

    def configured(self) -> bool:
        return bool(self.key)

    # ---- reads (free) ------------------------------------------------
    def board(self) -> Board | None:
        return self._board(self._load())

    def budget(self, now: dt.datetime | None = None) -> Budget:
        return self._budget(self._load(), now or dt.datetime.now())

    def history(self, days: int = 30,
                today: dt.date | None = None) -> dict[str, list[tuple[str, float]]]:
        """`{key: [(iso_day, value), …]}` for the tracked items, oldest first —
        the last fetch of each day that had one. Days nobody fetched are
        absent, not interpolated: a sparkline that draws a straight line
        through a gap is drawing a price nobody saw."""
        today = today or dt.date.today()
        start = (today - dt.timedelta(days=days - 1)).isoformat()
        hist = self._load().get("history") or {}
        out: dict[str, list[tuple[str, float]]] = {k: [] for k, *_ in TRACKED}
        for d, row in sorted(hist.items()):
            if d < start or not isinstance(row, dict):
                continue
            for k in out:
                if isinstance(row.get(k), (int, float)):
                    out[k].append((d, float(row[k])))
        return out

    def due(self, now: dt.datetime | None = None) -> bool:
        """Would `ensure_today()` make a request right now? Free to ask — it
        is how the HUD decides whether to start a thread at all."""
        return self.configured() and self._auto_due(
            self._load(), now or dt.datetime.now())

    def failure(self) -> dict:
        """The last attempt, if it failed: `{"at", "why", "status"}`."""
        return dict(self._load().get("failed") or {})

    # ---- writes (each one may spend a request) ----------------------
    def ensure_today(self, now: dt.datetime | None = None) -> Board | None:
        """Today's board, fetching it if nobody has yet today.

        Returns whatever is stored when V will not fetch — not configured,
        already tried and failed this hour, out of budget — and never raises
        for those: an automatic fetch failing is a state the reader draws,
        not an exception it handles. The reason is on `failure()`.
        """
        now = now or dt.datetime.now()
        if not self.configured():
            return self.board()
        state = self._load()
        if not self._auto_due(state, now):
            return self._board(state)
        with self._locked():
            state = self._load()           # another process may have won
            if not self._auto_due(state, now):
                return self._board(state)
            try:
                return self._fetch(state, now, kind="daily")
            except RatesError:
                return self._board(self._load())

    def refresh(self, now: dt.datetime | None = None, *,
                min_age: dt.timedelta | None = None) -> Board:
        """A fresh board, because a person asked. Raises `Refused` with the
        reason when the budget says no, `RatesError` when Navasan fails.

        `min_age` is the agent's extra brake: when the stored board is younger
        than that, it is returned as-is rather than spending a request.
        """
        now = now or dt.datetime.now()
        if not self.configured():
            raise NotConfigured(
                "no Navasan key — set navasan_api_key in "
                "~/.local/state/v/config.json or NAVASAN_API_KEY")
        with self._locked():
            state = self._load()
            current = self._board(state)
            if min_age is not None and current and now - current.fetched < min_age:
                return current
            b = self._budget(state, now)
            if b.why_not:
                raise Refused(b.why_not)
            # The first request of a day is the day's, whoever asked for it —
            # so a click at 08:00 does not cost a refresh *and* leave the
            # 10:00 job to spend a daily as well.
            kind = "refresh" if b.fetched_today else "daily"
            return self._fetch(state, now, kind=kind)

    # ---- internals ---------------------------------------------------
    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    @contextmanager
    def _locked(self):
        """One fetch at a time across every process V runs as. The HUD, the
        scheduler and an MCP server spawned for a turn can all decide the day
        has no board in the same second; only the first one asks."""
        import fcntl
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _board(state: dict) -> Board | None:
        latest = state.get("latest")
        if not isinstance(latest, dict):
            return None
        try:
            fetched = dt.datetime.fromisoformat(latest["at"])
            quotes = parse(latest.get("items") or {})
        except (KeyError, TypeError, ValueError, RatesError):
            return None
        return Board(fetched=fetched, quotes=quotes,
                     kind=str(latest.get("kind") or "daily"))

    @staticmethod
    def _ledger(state: dict, now: dt.datetime) -> list[dict]:
        since = now - dt.timedelta(days=WINDOW_DAYS)
        out = []
        for r in state.get("spent") or []:
            try:
                at = dt.datetime.fromisoformat(r["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if at > since:
                out.append({**r, "_at": at})
        return out

    def _budget(self, state: dict, now: dt.datetime) -> Budget:
        rows = self._ledger(state, now)
        counted = [r for r in rows if r.get("counted", True)]
        manual = sum(1 for r in counted if r.get("kind") == "refresh")
        board = self._board(state)
        today = bool(board and board.fetched.date() == now.date())
        b = Budget(used=len(counted), manual=manual, fetched_today=today)
        last = max((r["_at"] for r in counted), default=None)
        if not self.configured():
            b.why_not = "no Navasan key configured"
        elif b.used >= CAP:
            b.why_not = (f"the month's budget is spent — {b.used} of {CAP} "
                         f"requests in the last {WINDOW_DAYS} days")
        elif today and manual >= MANUAL_BUDGET:
            b.why_not = (f"all {MANUAL_BUDGET} refreshes for this "
                         f"{WINDOW_DAYS}-day window are used; the daily "
                         f"fetch still runs")
        elif last and now - last < COOLDOWN:
            b.next_refresh = last + COOLDOWN
            b.why_not = (f"last request was {int((now - last).total_seconds() // 60)}"
                         f" min ago — Navasan's free plan moves every 2 h, so "
                         f"the next refresh opens at "
                         f"{b.next_refresh.strftime('%H:%M')}")
        return b

    def _auto_due(self, state: dict, now: dt.datetime) -> bool:
        """Is today's automatic fetch still owed, and allowed?"""
        board = self._board(state)
        if board and board.fetched.date() == now.date():
            return False
        rows = [r for r in self._ledger(state, now)
                if r["_at"].date() == now.date() and r.get("kind") == "daily"]
        tried = [r for r in rows if r.get("counted", True)]
        if len(tried) >= AUTO_TRIES:
            return False
        if any(r.get("fatal") for r in rows):
            return False
        if rows and now - max(r["_at"] for r in rows) < AUTO_RETRY:
            return False
        used = sum(1 for r in self._ledger(state, now) if r.get("counted", True))
        return used < CAP

    def _fetch(self, state: dict, now: dt.datetime, *, kind: str) -> Board:
        """One request, recorded whatever happens. Caller holds the lock."""
        import httpx
        row = {"at": now.isoformat(timespec="seconds"), "kind": kind,
               "counted": True, "ok": False}
        try:
            try:
                resp = httpx.get(LATEST, params={"api_key": self.key},
                                 timeout=TIMEOUT,
                                 headers={"Accept": "application/json"})
            except (httpx.ConnectError, httpx.UnsupportedProtocol) as exc:
                # Never left the machine, so it cost nothing — but say which
                # kind of nothing, without the URL `str(exc)` would carry.
                row["counted"] = False
                raise RatesError(f"Navasan unreachable "
                                 f"({type(exc).__name__})") from None
            except httpx.HTTPError as exc:
                raise RatesError(f"Navasan did not answer "
                                 f"({type(exc).__name__})") from None
            row["status"] = resp.status_code
            if resp.status_code in (401, 403):
                row["fatal"] = True
                raise RatesError(f"Navasan refused the key ({resp.status_code})"
                                 f" — check navasan_api_key")
            if resp.status_code == 429:
                row["fatal"] = True
                raise RatesError("Navasan says the quota is used up (429)")
            if resp.status_code != 200:
                raise RatesError(f"Navasan answered {resp.status_code}: "
                                 f"{_scrub(resp.text[:120], self.key)}")
            try:
                payload = resp.json()
            except ValueError:
                raise RatesError("Navasan answered with something that is not "
                                 "JSON") from None
            quotes = parse(payload)
            row["ok"] = True
        except RatesError as exc:
            row["why"] = _scrub(str(exc), self.key)
            state["failed"] = {"at": row["at"], "why": row["why"],
                               "status": row.get("status")}
            self._record(state, row, now)
            raise RatesError(row["why"]) from None
        # Stored as Navasan sent it (only the readable rows), so a later
        # reader re-parses it the same way and a new symbol needs no migration.
        state["latest"] = {
            "at": row["at"], "kind": kind,
            "items": {k: payload[k] for k in quotes}}
        state.pop("failed", None)
        hist = state.setdefault("history", {})
        hist[now.date().isoformat()] = {
            k: quotes[k].value for k, *_ in TRACKED if k in quotes}
        cutoff = (now.date() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        state["history"] = {d: v for d, v in hist.items() if d >= cutoff}
        self._record(state, row, now)
        return Board(fetched=now, quotes=quotes, kind=kind)

    def _record(self, state: dict, row: dict, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(days=LEDGER_DAYS)
        kept = []
        for r in state.get("spent") or []:
            try:
                if dt.datetime.fromisoformat(r["at"]) > cutoff:
                    kept.append(r)
            except (KeyError, TypeError, ValueError):
                continue
        kept.append(row)
        state["spent"] = kept
        self._save(state)


# --------------------------------------------------------------------------
# words


def format_value(q: Quote) -> str:
    if q.unit == "USD":
        return f"${q.value:,.2f}"
    return f"{q.value:,.0f} T"


def format_change(q: Quote) -> str:
    if q.change is None:
        return ""
    if q.change == 0:
        return "unchanged since yesterday"
    arrow = "▲" if q.change > 0 else "▼"
    amount = (f"${abs(q.change):,.2f}" if q.unit == "USD"
              else f"{abs(q.change):,.0f} T")
    pct = f" ({q.pct:+.2f}%)" if q.pct is not None else ""
    return f"{arrow} {amount}{pct} since yesterday"
