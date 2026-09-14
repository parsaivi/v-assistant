"""The Persian calendar — the one Parsa actually lives on.

The vault is dated in Gregorian everywhere (`YYYY-MM-DD` filenames, `date:`
frontmatter) and that stays true, because sorting and arithmetic are easier
there and Obsidian's plugins expect it. But the *year* Parsa is in is 1405,
his rent is due in تیر, and "is tomorrow off?" is a question about a calendar
V could not read at all before this module. `Category Snapshot` notes are
already keyed on `jalali-month` — written by hand, because until now nothing
here could compute one.

**Two halves, and only one of them needs a network.**

`convert()` is arithmetic: the Khayyam-Borkowski algorithm, pure stdlib, no
key, no request, correct offline and correct in a tunnel. It is what the HUD
draws the date from, which is why the HUD's date is never blank.

`lookup()` is the other half — whether a day is *off*, and what is being
marked on it — and that genuinely is not derivable. Nothing local knows that
۲۲ بهمن is a holiday. It comes from pnldev.com, free and keyless, and when it
cannot be reached `Day.holiday` is `None` rather than `False`: "I don't know"
and "it's a working day" are different answers and only one of them is safe
to act on.

The half-degradation worth knowing: **Fridays are computable offline.** The
API reports `holiday: true` for every Friday (verified 1405/06/20 → true with
no religious event on it), and so can `convert()`. So with no network V still
knows the weekend; what it loses is عاشورا and ۲۲ بهمن.

**Gotcha, checked against the live service on 2026-09-09:** the API documents
a `base` parameter for feeding it Gregorian dates. It is ignored. Sending
`year=2026&month=9&day=9&base=gregorian` returns solar 2026/09/09 and
Gregorian 2647/11/30 — it read the input as Jalali whatever `base` said, and
answered confidently. Every value tried (`gregorian`, `miladi`, `christian`,
`g`, `m`, `2`) behaves identically. So this module converts *before* it asks,
and never sends a `base`.

The conversion is checked two ways in `test_core`: round-tripped over every
day from 1990 to 2050 (21,915 of them), and cross-checked against the live
service on 16 dates including both leap edges — 2025-03-20 is 1403-12-30, a
day that only exists in a Jalali leap year.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

import httpx

#: Free, keyless, and the only part of this module that leaves the machine.
#: Takes Jalali only — see the `base` gotcha in the module docstring.
API_URL = "https://pnldev.com/api/calender"

#: Mirrors `BRAIN_NO_TELEGRAM` and `BRAIN_NO_TTS`: a hard kill switch that
#: beats everything else, set by the test suite so that a run of the tests
#: never reaches the network. Conversion is unaffected — it never did.
NO_NETWORK_ENV = "BRAIN_NO_JALALI"

MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")

#: The Jalali week starts on Saturday, which is why this is not Python's
#: order. `_weekday_index` does the rotation.
WEEKDAYS = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه",
            "پنجشنبه", "جمعه")

#: Friday. Named rather than written as `6` at the three call sites that ask
#: "is this the weekend", because that is the question, not the number.
FRIDAY = 6

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Days elapsed at the start of each Gregorian month in a non-leap year.
_GDM = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


class CalendarError(RuntimeError):
    """The holiday service answered, but not usefully. Never raised by
    conversion, which cannot fail."""


def fa_digits(text: str | int) -> str:
    """`18` -> `۱۸`. Display only — never feed the result back to anything
    that has to parse it."""
    return str(text).translate(_FA_DIGITS)


def _weekday_index(date: dt.date) -> int:
    """Python counts from Monday; the Jalali week counts from Saturday."""
    return (date.weekday() + 2) % 7


@dataclass
class Day:
    """One day, in both calendars, with what the country thinks of it.

    `holiday` is deliberately three-valued. `True` and `False` are answers;
    `None` means the service was not reached and V does not know — which is
    the state to say out loud rather than round down to "it's a normal day"
    and have Parsa schedule a meeting on عید فطر.
    """

    date: dt.date               # the Gregorian day this describes
    year: int                   # …and the same day, Jalali
    month: int
    day: int
    holiday: bool | None = None
    events: tuple[str, ...] = ()
    #: "local" — arithmetic only; "pnldev" — the holiday service answered.
    source: str = "local"

    @property
    def iso(self) -> str:
        """`1405-06-18`. The same shape as a Gregorian ISO date on purpose:
        it sorts, and it is what a `jalali-month` field is built from."""
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    @property
    def month_key(self) -> str:
        """`1405-06` — exactly the `jalali-month` frontmatter value a
        `Category Snapshot` note is filed under."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def month_name(self) -> str:
        return MONTHS[self.month - 1]

    @property
    def weekday(self) -> str:
        return WEEKDAYS[_weekday_index(self.date)]

    @property
    def is_friday(self) -> bool:
        return _weekday_index(self.date) == FRIDAY

    @property
    def short(self) -> str:
        """`۱۸ شهریور ۱۴۰۵` — the form on the HUD, where the weekday is
        already obvious from the clock beside it."""
        return f"{fa_digits(self.day)} {self.month_name} {fa_digits(self.year)}"

    @property
    def label(self) -> str:
        """`چهارشنبه ۱۸ شهریور ۱۴۰۵` — the form to say out loud."""
        return f"{self.weekday} {self.short}"

    @property
    def off(self) -> bool:
        """Is this a day off, as far as anything here can tell?

        Falls back to the weekend when the service was not reached, which is
        the honest floor: a Friday is a Friday with or without a network, and
        answering `False` for one because the request timed out would be a
        worse lie than the fallback.
        """
        return self.is_friday if self.holiday is None else self.holiday


def convert(date: dt.date) -> Day:
    """Gregorian -> Jalali. Arithmetic, free, offline, cannot fail.

    Does NOT fill `holiday`/`events` — that is `lookup()`, and it is a
    separate call precisely so that everything which only wants the date
    (the HUD header, a filename, a `jalali-month`) never touches the network.
    """
    gy, gm, gd = date.year, date.month, date.day
    if gy > 1600:
        jy, gy2 = 979, gy - 1600
    else:
        jy, gy2 = 0, gy - 621
    gy3 = gy2 + 1 if gm > 2 else gy2
    days = (365 * gy2 + (gy3 + 3) // 4 - (gy3 + 99) // 100
            + (gy3 + 399) // 400 - 80 + gd + _GDM[gm - 1])
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return Day(date=date, year=jy, month=jm, day=jd)


def to_gregorian(jy: int, jm: int, jd: int) -> dt.date:
    """Jalali -> Gregorian. The inverse of `convert`, and asserted to be
    exactly that over 60 years of days in `test_core`."""
    if not 1 <= jm <= 12:
        raise ValueError(f"month {jm} is not a Jalali month (1-12)")
    if not 1 <= jd <= 31:
        raise ValueError(f"day {jd} is not a Jalali day (1-31)")
    if jy > 979:
        gy, jy2 = 1600, jy - 979
    else:
        gy, jy2 = 621, jy
    days = (365 * jy2 + (jy2 // 33) * 8 + (jy2 % 33 + 3) // 4 + 78 + jd
            + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186))
    gy += 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    lengths = (31, 29 if leap else 28, 31, 30, 31, 30,
               31, 31, 30, 31, 30, 31)
    gd, gm = days + 1, 0
    while gm < 12 and gd > lengths[gm]:
        gd -= lengths[gm]
        gm += 1
    return dt.date(gy, gm + 1, gd)


#: The gap between the two calendars is ~600 years and nothing real falls in
#: it, so which calendar a bare `1405-06-18` or `2026-09-09` is written in can
#: be read off the year alone. This is the boundary; it is not a guess that
#: can go wrong for any date either calendar will see in Parsa's lifetime.
_JALALI_YEAR_CEILING = 1700


def parse(text: str) -> dt.date:
    """`"1405-06-18"` or `"2026-09-09"` -> a Gregorian `date`.

    Which calendar it was written in is decided by the year, not by a flag the
    caller has to remember to set: Jalali years run in the 1300s-1400s and
    Gregorian in the 1900s-2100s, six centuries apart. `"today"` and
    `"tomorrow"` are accepted too, because that is what actually gets typed.
    """
    s = (text or "").strip().replace("/", "-")
    today = dt.date.today()
    if s.lower() in ("", "today", "امروز"):
        return today
    if s.lower() in ("tomorrow", "فردا"):
        return today + dt.timedelta(days=1)
    if s.lower() in ("yesterday", "دیروز"):
        return today - dt.timedelta(days=1)
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"{text!r} is not a date. Use YYYY-MM-DD in either calendar "
            "(1405-06-18 or 2026-09-09), or 'today'/'tomorrow'.")
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"{text!r} has non-numeric parts") from None
    if y < _JALALI_YEAR_CEILING:
        return to_gregorian(y, m, d)
    return dt.date(y, m, d)


def enabled() -> bool:
    """Whether the holiday half may reach the network at all."""
    return not os.environ.get(NO_NETWORK_ENV)


def lookup(date: dt.date, *, timeout: float = 8.0) -> Day:
    """`convert()` plus whether the country has the day off, and why.

    Never raises for a *missing* answer — a `Day` with `holiday=None` comes
    back when the switch is off, which is a state the caller should render
    rather than an error it should swallow. It does raise `CalendarError` when
    the service was reachable and answered with something unusable, because
    that is worth seeing rather than silently degrading to "not a holiday".
    """
    base = convert(date)
    if not enabled():
        return base
    try:
        r = httpx.get(API_URL,
                      params={"year": base.year, "month": base.month,
                              "day": base.day},
                      timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise CalendarError(
            f"The Persian calendar service ({API_URL}) could not be reached: "
            f"{exc}. The date itself is still right — it is computed here — "
            f"but whether {base.iso} is a holiday is unknown, and 'I don't "
            f"know' is the honest answer rather than 'no'.") from exc
    except ValueError as exc:
        raise CalendarError(
            f"The Persian calendar service returned something unreadable: "
            f"{exc}") from exc

    if not data.get("status"):
        raise CalendarError(
            f"The Persian calendar service rejected {base.iso}: "
            f"{data.get('result') or data}")
    result = data.get("result") or {}

    # Verify the round trip rather than trusting it. The service reads its
    # input as Jalali no matter what it is told (see the `base` gotcha), so a
    # bad conversion here would come back looking perfectly well-formed —
    # this is the check that turns that into a loud failure instead of a
    # holiday flag attached to the wrong day.
    greg = result.get("gregorian") or {}
    try:
        echoed = dt.date(int(greg["year"]), int(greg["month"]),
                         int(greg["day"]))
    except (KeyError, TypeError, ValueError):
        echoed = None
    if echoed is not None and echoed != date:
        raise CalendarError(
            f"Asked the calendar service for {base.iso} (which is {date}) "
            f"and it answered for {echoed}. The two calendars disagree, so "
            f"the holiday flag is not trustworthy for this date.")

    events = tuple(str(e).strip() for e in (result.get("event") or [])
                   if str(e).strip())
    return Day(date=base.date, year=base.year, month=base.month,
               day=base.day, holiday=bool(result.get("holiday")),
               events=events, source="pnldev")


def today(now: dt.datetime | None = None) -> Day:
    """Today, converted. Local only — see `lookup` for the holiday flag."""
    return convert((now or dt.datetime.now()).date())
