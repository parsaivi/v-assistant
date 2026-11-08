"""The standing morning briefing — one push, at a fixed time, unasked.

`engine.brief()` has always assembled the right thing: calendar, shortlist,
reminders, nudges, merged into one message. What it never had was a way to
reach you. It only ran inside `cli brief`, or inside a `cli scheduler` process
someone had remembered to start, and printed to that process's stdout. So the
brief existed and was, in practice, something you had to ask for — which is the
standup-every-morning habit it was meant to replace.

This module is the delivery half, and the whole design is about earning the
right to interrupt:

**One push per day, at 10:00.** `state/briefing.json` records the date, so the
cron entry and a running scheduler cannot both fire it, and a laptop that wakes
at 11 gets the brief once rather than not at all.

**Thin mornings stay silent.** This is the tradeoff being paid down. A briefing
that pushes "3 broken wikilinks" at you every morning trains you to dismiss it,
and then it is worth less than nothing, because the morning it *does* matter it
gets dismissed too. `signal()` scores what the brief actually contains; a
morning with no events, no reminders, no urgent nudge and fewer than two live
tasks is not pushed at all. It is recorded as skipped, and `cli briefing
--status` will tell you it was.

**Two channels, one of which is durable.** notify-send is the interruption and
is allowed to be lossy; the daily note is the record and is not. Missing the
popup costs nothing.

Delivery is best-effort by construction: no notification daemon, no DISPLAY, no
`notify-send` binary at all — none of those may stop the note being written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from . import provenance
from .config import Settings, settings as default_settings

# When the standing briefing runs. Signed off at 10:00 daily, weekends
# included — a birthday or an overdue assignment does not care what day it is.
DEFAULT_TIME = dt.time(10, 0)

# The marker that makes the crontab entry findable, so installing twice
# replaces the line instead of stacking a second one beside it.
CRON_MARKER = "# V morning briefing (v/core/briefing.py)"


@dataclass
class Briefing:
    day: dt.date
    text: str
    counts: dict = field(default_factory=dict)
    delivered: bool = False
    channels: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def signal(self) -> int:
        return signal(self.counts)

    @property
    def thin(self) -> bool:
        return self.signal <= 0

    def headline(self) -> str:
        """One line for the notification title — what today actually holds."""
        c = self.counts
        bits = []
        if c.get("events"):
            bits.append(f"{c['events']} event{_s(c['events'])}")
        if c.get("tasks"):
            bits.append(f"{c['tasks']} task{_s(c['tasks'])}")
        if c.get("reminders"):
            bits.append(f"{c['reminders']} reminder{_s(c['reminders'])}")
        if c.get("urgent"):
            bits.append(f"{c['urgent']} needing you")
        return " · ".join(bits) or "nothing pressing"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def signal(counts: dict) -> int:
    """How much the brief is worth interrupting for.

    Weighted, not counted. Anything time-bound — a meeting, a reminder — is
    worth a push on its own, and so is a nudge urgent enough to act on today.
    Tasks are not: there are always tasks, so a lone one is the background
    state of the vault rather than news, and two or more is a shortlist.
    FYI-level nudges score nothing at all; they are what the brief is *for*
    once you are reading it, never a reason to make you read it.
    """
    return (counts.get("events", 0)
            + counts.get("reminders", 0)
            + 2 * counts.get("urgent", 0)
            + (1 if counts.get("tasks", 0) >= 2 else 0))


# ---- the mute switch -------------------------------------------------
def muted(settings: Settings | None = None,
          now: dt.datetime | None = None) -> bool:
    """`/quiet` in the REPL, read from disk on every check.

    The switch is thrown in one process and honoured in another — the cron run
    is not the REPL — so a value cached at start-up would mean the only way to
    silence V is to restart the thing doing the talking.
    """
    s = settings or default_settings
    now = now or dt.datetime.now()
    try:
        prefs = json.loads(
            (s.state_dir / "surface.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(prefs.get("quiet_until", "")) > now.isoformat()


# ---- state -----------------------------------------------------------
def _state_path(s: Settings):
    return s.state_dir / "briefing.json"


def last_run(settings: Settings | None = None) -> dict:
    s = settings or default_settings
    try:
        return json.loads(_state_path(s).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _record(s: Settings, brief: Briefing) -> None:
    try:
        _state_path(s).parent.mkdir(parents=True, exist_ok=True)
        _state_path(s).write_text(json.dumps({
            "day": brief.day.isoformat(),
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "delivered": brief.delivered,
            "channels": brief.channels,
            "reason": brief.reason,
            "signal": brief.signal,
            "counts": brief.counts,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---- channels --------------------------------------------------------
def _plain(text: str) -> str:
    """Markdown → something a notification bubble can show honestly."""
    text = re.sub(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]",
                  lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    return text


def notify(title: str, body: str, *, urgency: str = "normal") -> bool:
    """Desktop notification. False when there is no one to notify.

    Cron does not inherit a session bus, which is the usual reason a
    notification works by hand and silently does nothing at 10:00. Both the
    display and the bus address are reconstructed here rather than being made
    the crontab line's problem.
    """
    # The test suite builds real Engines and really calls `catch_up`; without
    # this, running it pops desktop notifications at whoever is at the machine.
    if os.environ.get("BRAIN_NO_NOTIFY"):
        return False
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    try:
        proc = subprocess.run(
            ["notify-send", "--app-name=V", f"--urgency={urgency}",
             "--expire-time=20000", title, body],
            capture_output=True, text=True, timeout=15, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _daily_note(engine, brief: Briefing, when: dt.datetime) -> str | None:
    """Write the brief into today's note, as a foldable callout.

    Under `daily_log_heading`, deliberately: that heading exists in
    `05 - Templates/Daily Note.md`, and appending under one that doesn't is how
    you end up with a stray section bolted to the bottom of every daily note.
    """
    rel = engine.capture.ensure_daily(brief.day)
    quoted = "\n".join(f"> {line}" if line else ">"
                       for line in brief.text.splitlines())
    block = (f"> [!info]- Morning brief · {when.strftime('%H:%M')}\n{quoted}")
    engine.vault.append(rel, block, heading=engine.s.daily_log_heading,
                        summary=f"morning brief {brief.day}",
                        # Assembled by rules, but it is V's reading of the
                        # vault — priorities it ranked, not ones he set.
                        source=provenance.V)
    return rel


# ---- the job ---------------------------------------------------------
def build(engine, now: dt.datetime | None = None) -> Briefing:
    """Assemble today's brief. No delivery, no state written — pure."""
    now = now or dt.datetime.now()
    resp = engine.brief(now)
    return Briefing(day=now.date(), text=resp.text,
                    counts=dict(resp.data or {}))


def run(engine, *, now: dt.datetime | None = None, force: bool = False,
        channels: tuple[str, ...] = ("notify", "daily", "telegram")) -> Briefing:
    """Build, decide, deliver, record. The one entry point cron and the
    scheduler both call — which is what keeps them from pushing twice."""
    now = now or dt.datetime.now()
    s = engine.s
    brief = build(engine, now)

    state = last_run(s)
    if not force and state.get("day") == brief.day.isoformat() \
            and state.get("delivered"):
        brief.reason = f"already delivered at {state.get('at', '?')}"
        return brief

    if not force and muted(s, now):
        brief.reason = "quiet mode"
        _record(s, brief)
        return brief

    if not force and brief.thin:
        # Recorded, not delivered — so `--status` can say why the morning was
        # silent, and so a thin 10:00 doesn't mean a second attempt at 10:30.
        brief.reason = "thin signal — nothing worth interrupting for"
        _record(s, brief)
        return brief

    if "notify" in channels:
        title = f"Good morning · {brief.headline()}"
        if notify(title, _plain(brief.text)[:900]):
            brief.channels.append("notify-send")
    if "daily" in channels:
        try:
            rel = _daily_note(engine, brief, now)
            brief.channels.append(f"daily note ({rel})")
        except Exception as exc:
            brief.reason = f"daily note failed: {exc}"
    if "telegram" in channels:
        # A no-op, not a failure path, when no bot is configured — `send_message`
        # returns False immediately without an HTTP call. Silent by design: a
        # briefing that requires Telegram to be set up would break every vault
        # that doesn't want a bot.
        from .telegram import send_message
        if send_message(s, s.telegram_owner_id, brief.text):
            brief.channels.append("telegram")

    brief.delivered = bool(brief.channels)
    if not brief.delivered and not brief.reason:
        brief.reason = "no channel available"
    _record(s, brief)
    return brief


# ---- cron ------------------------------------------------------------
def cron_line(settings: Settings | None = None,
              at: dt.time = DEFAULT_TIME) -> str:
    """The crontab entry, pinned to the interpreter you install it with.

    cron fires on the *system* zone, which on this machine is Asia/Tehran — the
    same zone `config.TIMEZONE` pins the process to. That is worth stating
    because the shell V is usually launched from is not: `~/.bashrc` carries a
    `TZ=Asia/Singapore` alias, so `date` in that terminal reads 4h30m ahead of
    what this entry will actually fire at.
    """
    s = settings or default_settings
    return (f"{at.minute} {at.hour} * * * cd {s.vault} && "
            f"{sys.executable} -m v.cli briefing --push "
            f">> {s.log_dir / 'briefing.log'} 2>&1  {CRON_MARKER}")


def _crontab(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["crontab", *args], input=stdin, capture_output=True,
                          text=True, timeout=15)


def current_cron() -> list[str]:
    try:
        proc = _crontab("-l")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def installed(settings: Settings | None = None) -> str | None:
    for line in current_cron():
        if CRON_MARKER in line:
            return line
    return None


def install_cron(settings: Settings | None = None,
                 at: dt.time = DEFAULT_TIME) -> str:
    """Install (or replace) the crontab entry. Idempotent, by the marker."""
    s = settings or default_settings
    lines = [l for l in current_cron() if CRON_MARKER not in l]
    lines.append(cron_line(s, at))
    try:
        proc = _crontab("-", stdin="\n".join(lines).strip() + "\n")
    except FileNotFoundError:
        raise RuntimeError("crontab is not installed on this machine")
    if proc.returncode != 0:
        raise RuntimeError(f"crontab failed: {(proc.stderr or '').strip()}")
    return lines[-1]


def uninstall_cron() -> bool:
    lines = current_cron()
    kept = [l for l in lines if CRON_MARKER not in l]
    if len(kept) == len(lines):
        return False
    body = "\n".join(kept).strip()
    _crontab("-", stdin=body + "\n" if body else "\n")
    return True
