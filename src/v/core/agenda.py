"""Today's calendar, for the morning brief.

`daily_calendar.py` has sat at the vault root doing this correctly since June —
fetching the private iCal feed, expanding RRULEs, honouring EXDATEs — and
nothing in the assistant ever called it. The brief was shipping without the one
thing every design note puts first in it. This wraps that script rather than
reimplementing it: recurrence expansion is the kind of code you write once and
never want two of.

Results are cached for the day. The brief is the only caller and it runs once
each morning, but a scheduler that retries after a crash shouldn't refetch a
remote feed each time, and a laptop that woke up without wifi should still
show what it saw earlier.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from dataclasses import dataclass

from .config import Settings, settings as default_settings


@dataclass
class Event:
    label: str          # "17:30–18:30" or "all-day"
    summary: str
    location: str = ""
    all_day: bool = False

    def render(self) -> str:
        where = f" _({self.location})_" if self.location else ""
        return f"· {self.label} — {self.summary}{where}"


def _load_script(s: Settings):
    """Import daily_calendar.py from the vault root by path.

    It lives beside the notes rather than in this package, so a normal import
    won't find it, and moving it would break the standalone CLI usage that
    SCRIPTS.md documents.
    """
    path = s.vault / "daily_calendar.py"
    if not path.exists():
        return None
    if "daily_calendar" in sys.modules:
        return sys.modules["daily_calendar"]
    spec = importlib.util.spec_from_file_location("daily_calendar", path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_calendar"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


class Agenda:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    @property
    def _cache_path(self):
        return self.s.state_dir / "agenda.json"

    def configured(self) -> bool:
        mod = _load_script(self.s)
        return bool(mod and mod.get_sources(None))

    def events(self, day: dt.date | None = None, *,
               use_cache: bool = True) -> list[Event]:
        day = day or dt.date.today()
        if use_cache:
            cached = self._read_cache(day)
            if cached is not None:
                return cached

        mod = _load_script(self.s)
        if not mod:
            return []
        sources = mod.get_sources(None)
        if not sources:
            return []

        rows = []
        for src in sources:
            try:
                rows.extend(mod.collect_day_events(mod.read_source(src), day))
            except Exception:
                # A dead feed must not take the whole brief down with it — the
                # rest of the brief is local and still worth showing.
                continue
        # Same key `collect_day_events` sorts by, and it has to be: row[0] is
        # a naive `time.min` for an all-day event and an aware `timetz()` for a
        # timed one, so sorting on it alone raises "can't compare offset-naive
        # and offset-aware times" the moment a day holds both. The caller
        # catches that and moves on, which meant any day with a holiday on it
        # imported *none* of its meetings — silently. Ordering by the all-day
        # flag first keeps the two kinds from ever being compared.
        rows.sort(key=lambda r: (0 if r[1] else 1, r[0]))

        out = []
        for (_key, all_day, summary, location, start_s, end_s, _uid,
             _cal) in rows:
            label = "all-day" if all_day else (
                f"{start_s}–{end_s}" if end_s else start_s)
            out.append(Event(label, summary, location or "", all_day))
        self._write_cache(day, out)
        return out

    # ---- cache --------------------------------------------------------
    def _read_cache(self, day: dt.date) -> list[Event] | None:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if raw.get("day") != day.isoformat():
            return None
        return [Event(**e) for e in raw.get("events", [])]

    def _write_cache(self, day: dt.date, events: list[Event]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps({
                "day": day.isoformat(),
                "events": [e.__dict__ for e in events],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def next_event(self, now: dt.datetime | None = None) -> Event | None:
        """The next thing today that hasn't started yet."""
        now = now or dt.datetime.now()
        for e in self.events(now.date()):
            if e.all_day:
                continue
            try:
                hh, mm = e.label.split("–")[0].split(":")
                if dt.time(int(hh), int(mm)) > now.time():
                    return e
            except (ValueError, IndexError):
                continue
        return None
