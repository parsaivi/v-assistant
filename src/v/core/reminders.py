"""Reminders and scheduled jobs, in SQLite.

Two rules from the design doc, both load-bearing:

  1. Reminders are machinery, not content. They live in a table, not in
     frontmatter — otherwise firing one means scanning 600 files every minute.
  2. **Catch up on boot.** A two-hour outage must not silently eat reminders.
     This is the single most user-visible way a reminder bot can fail, and it is
     the one failure mode the current scheduled-messages hack does not have.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable

from . import index
from .config import Settings, settings as default_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    due        REAL NOT NULL,
    created    REAL NOT NULL,
    fired      REAL,
    cancelled  INTEGER DEFAULT 0,
    recurrence TEXT,
    source     TEXT,
    meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(due, fired, cancelled);

CREATE TABLE IF NOT EXISTS jobs (
    name      TEXT PRIMARY KEY,
    spec      TEXT NOT NULL,
    last_run  REAL,
    next_run  REAL,
    enabled   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS outbox (
    id      INTEGER PRIMARY KEY,
    kind    TEXT,
    body    TEXT,
    created REAL,
    sent    REAL
);
"""


@dataclass
class Reminder:
    id: int
    text: str
    due: dt.datetime
    created: dt.datetime
    fired: dt.datetime | None = None
    cancelled: bool = False
    recurrence: str | None = None
    source: str = "user"

    @property
    def overdue_by(self) -> dt.timedelta:
        return dt.datetime.now() - self.due

    def describe(self) -> str:
        when = self.due.strftime("%a %d %b %H:%M")
        rec = f" (every {self.recurrence})" if self.recurrence else ""
        return f"#{self.id} {when}{rec} — {self.text}"


class Reminders:
    def __init__(self, settings: Settings | None = None,
                 conn: sqlite3.Connection | None = None):
        self.s = settings or default_settings
        self.conn = conn or index.connect(self.s)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- creating ----------------------------------------------------
    def add(self, text: str, due: dt.datetime, *, recurrence: str | None = None,
            source: str = "user", meta: dict | None = None) -> Reminder:
        now = dt.datetime.now()
        cur = self.conn.execute(
            "INSERT INTO reminders (text, due, created, recurrence, source, meta)"
            " VALUES (?,?,?,?,?,?)",
            (text, due.timestamp(), now.timestamp(), recurrence, source,
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        self.conn.commit()
        return Reminder(cur.lastrowid, text, due, now, recurrence=recurrence,
                        source=source)

    def add_natural(self, text: str, *, now: dt.datetime | None = None,
                    source: str = "user") -> Reminder | None:
        """Parse a time expression out of plain text. No model involved."""
        now = now or dt.datetime.now()
        due, cleaned, recurrence = parse_when(text, now)
        if due is None:
            return None
        return self.add(cleaned, due, recurrence=recurrence, source=source)

    # ---- reading -----------------------------------------------------
    def pending(self) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE fired IS NULL AND cancelled=0"
            " ORDER BY due").fetchall()
        return [_row(r) for r in rows]

    def due_now(self, now: dt.datetime | None = None) -> list[Reminder]:
        """Everything that should have fired by now — including while we were down."""
        now = now or dt.datetime.now()
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE fired IS NULL AND cancelled=0"
            " AND due <= ? ORDER BY due", (now.timestamp(),)).fetchall()
        return [_row(r) for r in rows]

    def get(self, rid: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id=?",
                                (rid,)).fetchone()
        return _row(row) if row else None

    # ---- state transitions -------------------------------------------
    def mark_fired(self, reminder: Reminder, *, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now()
        self.conn.execute("UPDATE reminders SET fired=? WHERE id=?",
                          (now.timestamp(), reminder.id))
        if reminder.recurrence:
            nxt = advance(reminder.due, reminder.recurrence, now)
            if nxt:
                self.conn.execute(
                    "INSERT INTO reminders (text, due, created, recurrence, source, meta)"
                    " VALUES (?,?,?,?,?,'{}')",
                    (reminder.text, nxt.timestamp(), now.timestamp(),
                     reminder.recurrence, reminder.source),
                )
        self.conn.commit()

    def cancel(self, rid: int) -> bool:
        cur = self.conn.execute(
            "UPDATE reminders SET cancelled=1 WHERE id=? AND fired IS NULL", (rid,))
        self.conn.commit()
        return cur.rowcount > 0

    def snooze(self, rid: int, minutes: int) -> Reminder | None:
        r = self.get(rid)
        if not r:
            return None
        new_due = dt.datetime.now() + dt.timedelta(minutes=minutes)
        self.conn.execute("UPDATE reminders SET due=?, fired=NULL WHERE id=?",
                          (new_due.timestamp(), rid))
        self.conn.commit()
        return self.get(rid)

    # ---- catch-up ----------------------------------------------------
    def catch_up(self, *, now: dt.datetime | None = None,
                 grace_minutes: int = 0) -> list[Reminder]:
        """Reminders missed while the process was down. Call this on every boot."""
        now = now or dt.datetime.now()
        missed = [r for r in self.due_now(now)
                  if r.overdue_by > dt.timedelta(minutes=grace_minutes)]
        return missed

    # ---- outbox ------------------------------------------------------
    def queue(self, kind: str, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO outbox (kind, body, created) VALUES (?,?,?)",
            (kind, body, dt.datetime.now().timestamp()))
        self.conn.commit()
        return cur.lastrowid

    def unsent(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM outbox WHERE sent IS NULL ORDER BY created")]

    def mark_sent(self, oid: int) -> None:
        self.conn.execute("UPDATE outbox SET sent=? WHERE id=?",
                          (dt.datetime.now().timestamp(), oid))
        self.conn.commit()


def _row(r: sqlite3.Row) -> Reminder:
    return Reminder(
        r["id"], r["text"], dt.datetime.fromtimestamp(r["due"]),
        dt.datetime.fromtimestamp(r["created"]),
        dt.datetime.fromtimestamp(r["fired"]) if r["fired"] else None,
        bool(r["cancelled"]), r["recurrence"], r["source"] or "user",
    )


# ---------------------------------------------------------------------
# time parsing — deterministic, no model
# ---------------------------------------------------------------------
_REL = re.compile(r"\bin\s+(\d+)\s*(min|mins|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks)\b", re.I)
_AT = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_ON = re.compile(r"\bon\s+(\d{4}-\d{2}-\d{2})\b")
_EVERY = re.compile(r"\bevery\s+(day|week|month|morning|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def parse_when(text: str, now: dt.datetime | None = None
               ) -> tuple[dt.datetime | None, str, str | None]:
    """Return (due, cleaned_text, recurrence). Pure regex — free and predictable."""
    now = now or dt.datetime.now()
    original = text
    due: dt.datetime | None = None
    recurrence: str | None = None

    m = _EVERY.search(text)
    if m:
        unit = m.group(1).lower()
        recurrence = unit
        text = text[:m.start()] + text[m.end():]

    m = _REL.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "min": "minutes", "mins": "minutes", "minute": "minutes",
            "minutes": "minutes", "h": "hours", "hr": "hours", "hour": "hours",
            "hours": "hours", "d": "days", "day": "days", "days": "days",
            "w": "weeks", "week": "weeks", "weeks": "weeks",
        }[unit]
        due = now + dt.timedelta(**{delta: n})
        text = text[:m.start()] + text[m.end():]

    if due is None:
        m = _ON.search(text)
        base_date = None
        if m:
            base_date = dt.date.fromisoformat(m.group(1))
            text = text[:m.start()] + text[m.end():]
        low = text.lower()
        if "tomorrow" in low:
            base_date = (now + dt.timedelta(days=1)).date()
            text = re.sub(r"\btomorrow\b", "", text, flags=re.I)
        elif "tonight" in low:
            base_date = now.date()
            text = re.sub(r"\btonight\b", "", text, flags=re.I)
            due = dt.datetime.combine(base_date, dt.time(20, 0))
        elif "today" in low:
            base_date = now.date()
            text = re.sub(r"\btoday\b", "", text, flags=re.I)
        elif "next week" in low:
            base_date = (now + dt.timedelta(days=7)).date()
            text = re.sub(r"\bnext week\b", "", text, flags=re.I)
        else:
            for name, idx in _WEEKDAYS.items():
                if re.search(rf"\b{name}\b", low):
                    ahead = (idx - now.weekday()) % 7 or 7
                    base_date = (now + dt.timedelta(days=ahead)).date()
                    text = re.sub(rf"\b{name}\b", "", text, flags=re.I)
                    break

        m = _AT.search(text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            elif not ampm and hour < 8:
                hour += 12  # "at 7" in the evening is the common case
            day = base_date or now.date()
            due = dt.datetime.combine(day, dt.time(hour % 24, minute))
            if due <= now and base_date is None:
                due += dt.timedelta(days=1)
            text = text[:m.start()] + text[m.end():]
        elif base_date and due is None:
            due = dt.datetime.combine(base_date, dt.time(9, 0))

    if due is None and recurrence:
        due = advance(now, recurrence, now) or now + dt.timedelta(days=1)

    cleaned = re.sub(r"^(remind me( to| that)?|remember to|don'?t forget( to)?)\s*",
                     "", text, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")
    return due, (cleaned or original.strip()), recurrence


def advance(due: dt.datetime, recurrence: str, now: dt.datetime) -> dt.datetime | None:
    rec = recurrence.lower()
    if rec in ("day", "morning"):
        nxt = due + dt.timedelta(days=1)
    elif rec == "week":
        nxt = due + dt.timedelta(weeks=1)
    elif rec == "month":
        month = due.month % 12 + 1
        year = due.year + (due.month // 12)
        day = min(due.day, 28)
        nxt = due.replace(year=year, month=month, day=day)
    elif rec in _WEEKDAYS:
        nxt = due + dt.timedelta(days=7)
    else:
        return None
    while nxt <= now:
        nxt += dt.timedelta(days=1 if rec in ("day", "morning") else 7)
    return nxt
