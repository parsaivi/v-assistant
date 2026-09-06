"""hud.py — the wide desktop surface, in a browser on this machine.

`tui.py` draws §03 of the design brief: one window, one conversation, a side
pane. This is the other half of the same idea — everything V knows about today
on screen at once, which is the one thing a terminal window structurally
cannot do. Six panels of live state next to the conversation is a *layout*
problem before it is anything else, and at 80×24 there is no layout.

So it is a browser, and that decision carries three constraints worth stating
because each one had an obvious wrong answer:

**Loopback only, and its own surface.** `resolve_bind()` in `httpapi.py`
argues the general case; here it is not a choice — the socket binds
`127.0.0.1` and there is no flag to widen it, because unlike the phone
transport there is no mutual TLS underneath to make a wider bind survivable.
That is also why this is `persona.HUD` and not `persona.TUI`: a TTY is held by
the person sitting at it, while a loopback port is reachable by every process
on the box. EDITH is gated on "physically at the terminal" and she keeps that
gate — summoned here, `_agent_for` falls back to V and says so.

**The engine still owns its thread.** Same reason as `httpapi._Bridge`, and
that docstring is the long version: `Engine` holds a sqlite3 connection, an
HTTP server is a thread pool, and one conversation split across four sockets
is four strangers. The bridge here is a slimmer copy rather than an import
because that one hardcodes `surface=PHONE` and carries the phone's speech
spools, neither of which belongs on this surface.

**Nothing on the page is invented.** Every panel is a real read. The HUD polls
a snapshot that is SQL and Markdown end to end — `Response.cost` is 0.0 for
all of it and `test_hud` asserts that, for the same reason `glance()` is
asserted free: a dashboard on a second monitor redraws all day, and a
dashboard that bills per redraw is a dashboard you turn off.

The one place the design asks for something this surface does not have is the
microphone. The mockup's ambient-state list is a picker, six states you click
between. There is no mic on a browser tab, so rather than animate a lie the
states are *derived*, exactly as `tui.py` derives them: typing is `listening`,
a turn in flight is `thinking`, the seconds after an answer are `speaking`, a
turn that wrote to the vault is `writing`, and a turn that raised is `fault`.
The list is an indicator, not a control.

```
browser ──HTTP(loopback)──►  _Handler ──►  _Bridge ──►  Engine.handle
   ▲                             │                          │
   │                             ▼                          ▼
   └── SSE /api/events ◄──── Session ◄──── engine.progress (tool events)
```

`Session` is the part that is *not* behind the bridge, and that is deliberate:
while a turn is in flight the engine thread is blocked, so a snapshot poll
would sit in the queue behind it and time out. The aperture, the route strip
and the tool-bus ticker are all fed from `Session` instead, which the progress
callback writes to from whichever thread is running the turn. That is why the
HUD stays alive *during* a long answer rather than freezing until it lands.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import mimetypes
import queue
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import commands as commands_mod, index, persona as persona_mod
from .config import Settings, settings as default_settings
from .engine import Engine

#: The page, its stylesheet and its script, shipped as package data next to
#: this module. Not embedded in a Python string: a 600-line stylesheet inside
#: a heredoc is one no editor will ever highlight or lint.
ASSETS = Path(__file__).parent / "hud"

#: Not 8080, not 3000. High enough to need no privilege, odd enough not to
#: collide with the dev server of whatever else is open.
DEFAULT_PORT = 7717

#: A snapshot waits this long for the engine thread before the last good one
#: is served instead, stamped stale. Twenty seconds is `glance`'s number and
#: the reasoning is the same: a panel that waits a minute has already been
#: read as broken, and the previous answer with a timestamp on it is more
#: honest than a spinner.
SNAPSHOT_TIMEOUT = 20.0
#: A turn may take as long as the model takes.
CHAT_TIMEOUT = 600.0

#: How long an answer keeps the aperture in `speaking` before it falls back to
#: idle. Long enough to read as a state, short enough not to lie about it.
SPEAK_HOLD = 3.0

#: Panel sizes. The design's own counts, which are also about one screen.
N_AGENDA = 8
N_TASKS = 6
N_DEADLINES = 4
N_MEMS = 8
N_NUDGES = 4
N_MAIL = 4
N_HITS = 8
#: The tool-bus ticker and the sparkline behind it.
N_TICKER = 24

#: Gmail is the one panel that is not free — it is a network round trip to
#: Google. So it is cached, and the HUD redraws from the cache rather than
#: asking again on every poll.
MAIL_TTL = 300.0
#: The drift walk is one `stat` per note. Cheap, but not free at 3,400 notes
#: times a two-second poll.
DRIFT_TTL = 30.0

#: Which arc of the aperture a tool call lights. The design draws four —
#: VLT · CAL · MSG · MEM — and these are the real MCP tool names behind them.
_ARCS = (
    ("mem", ("vault_memory", "vault_voice")),
    ("cal", ("gcal_", "vault_calendar", "vault_remind", "vault_schedule")),
    ("msg", ("gmail_", "vault_send_file")),
    ("vault", ("vault_", "mcp__vault__")),
)


def arc_for(tool: str) -> str:
    """The aperture arc a tool call belongs to, or "" for one that has none.

    Ordered, not a dict: `vault_memory_write` is a memory call and also starts
    with `vault_`, and the first prefix that matches has to be the specific
    one. A dict here read as `vault` and the MEM arc never lit.
    """
    name = tool.rsplit("__", 1)[-1] if "__" in tool else tool
    for arc, prefixes in _ARCS:
        if any(name.startswith(p) or tool.startswith(p) for p in prefixes):
            return arc
    return ""


# --------------------------------------------------------------------------
# the session — live state, in front of the engine thread


@dataclass
class Call:
    """One tool call, as it goes out and again as it comes back."""

    at: float
    tool: str
    detail: str = ""
    arc: str = ""
    ms: float = 0.0
    status: str = "…"

    def json(self) -> dict:
        return {"at": dt.datetime.fromtimestamp(self.at).strftime("%H:%M:%S"),
                "call": self.label, "arc": self.arc,
                "ms": round(self.ms), "status": self.status}

    @property
    def label(self) -> str:
        """The call as a person would say it, not as MCP spells it.

        `cli._tool_label` does the same job for the spinner; this one keeps the
        argument, because the route strip has room for it and "vault_search"
        without its query is the half of the line that says nothing.
        """
        name = self.tool
        if name.startswith("mcp__claude-in-chrome__"):
            name = "chrome · " + name.split("__")[-1]
        elif "__" in name:
            name = name.rsplit("__", 1)[-1]
        return f"{name}({self.detail})" if self.detail else name


@dataclass
class Turn:
    """One exchange, kept as content rather than as markup — same reason
    `tui.Entry` is: the browser re-renders on every state change, and a turn
    stored as HTML can only be re-rendered by trusting whatever built it."""

    role: str                  # operator · v
    text: str
    at: float
    route: str = ""
    cites: list[str] = field(default_factory=list)
    cost: float = 0.0
    ms: float = 0.0
    model: str = ""
    confidence: float | None = None
    state: str = "speaking"    # speaking · unsure · refused · degraded

    def json(self) -> dict:
        return {
            "role": self.role, "text": self.text, "route": self.route,
            "time": dt.datetime.fromtimestamp(self.at).strftime("%H:%M"),
            "cites": self.cites, "cost": self.cost, "ms": round(self.ms),
            "model": self.model, "confidence": self.confidence,
            "state": self.state,
        }


class Session:
    """What is true *now*, readable without touching the engine thread.

    Everything here is written either by the HTTP thread handling a request or
    by `engine.progress` on whichever thread is running the turn, and read by
    the SSE loop. One lock, held briefly, rather than one per field: the
    contention is a handful of writes a second against one reader.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.turns: list[Turn] = []
        self.calls: list[Call] = []
        self.inflight: dict[str, Call] = {}
        self.spent = 0.0
        self.n_turns = 0
        self.n_local = 0
        self.writes: list[tuple[str, float, str, float]] = []
        self.busy = False
        self.typing = 0.0          # last keystroke, from the client
        self.spoke_at = 0.0        # when the last answer landed
        self.fault = ""
        self.last_ms = 0.0
        self.last_model = ""
        self.step = ""
        self._subs: list[queue.Queue] = []
        self._seq = 0

    # ---- the ambient state, derived rather than picked ----------------
    def state(self, now: float | None = None) -> tuple[str, str]:
        """`(name, detail)`. See the module docstring for why this is not a
        setter: there is no microphone on this surface, so every state here is
        read off something that actually happened."""
        now = now or time.time()
        if self.fault:
            return "error", self.fault
        if self.busy:
            n = len(self.inflight)
            detail = self.step or "routing"
            if n:
                detail = f"{detail} · {n} tool{'s' if n != 1 else ''} in flight"
            return "thinking", detail
        if self.writes and now - self.writes[0][3] < SPEAK_HOLD:
            return "writing", f"wrote {self.writes[0][0]}"
        if now - self.spoke_at < SPEAK_HOLD:
            return "speaking", f"{self.last_model or 'local'} · answer landed"
        if now - self.typing < 2.0:
            return "listening", "keyboard · composing"
        return "idle", "ambient · nothing running"

    def tools_lit(self) -> list[str]:
        """Which arcs are lit. In flight while a turn runs; for a few seconds
        after it lands, the arcs that turn actually used — otherwise the
        picture goes dark at exactly the moment you look up at it."""
        with self.lock:
            if self.inflight:
                return sorted({c.arc for c in self.inflight.values() if c.arc})
            if time.time() - self.spoke_at < SPEAK_HOLD:
                recent = [c for c in self.calls[:8]
                          if c.at >= self.spoke_at - 30]
                return sorted({c.arc for c in recent if c.arc})
        return []

    # ---- the progress callback ---------------------------------------
    def progress(self, ev) -> None:
        """Handed to `engine.progress`. Never allowed to raise — `_emit`
        swallows it anyway, but a surface that loses an answer because its
        picture had a bug is the failure that rule exists to prevent."""
        try:
            self._progress(ev)
        except Exception:
            pass

    def _progress(self, ev) -> None:
        now = time.time()
        if ev.kind == "tool":
            call = Call(at=now, tool=ev.tool, detail=ev.detail,
                        arc=arc_for(ev.tool))
            with self.lock:
                # A second call to the same tool supersedes the first in the
                # in-flight map; the first is already in `calls` and keeps its
                # own row. Keyed by tool because the stream gives no call id.
                prev = self.inflight.get(ev.tool)
                if prev is not None:
                    prev.ms = (now - prev.at) * 1000
                    prev.status = "OK"
                self.inflight[ev.tool] = call
                self.calls.insert(0, call)
                del self.calls[N_TICKER:]
                self.step = call.label
        elif ev.kind == "text":
            self._settle("OK")
            with self.lock:
                self.step = "writing the answer"
        elif ev.kind == "limit":
            with self.lock:
                self.step = f"plan window: {ev.detail}"
        self.publish()

    def _settle(self, status: str) -> None:
        now = time.time()
        with self.lock:
            for call in self.inflight.values():
                call.ms = (now - call.at) * 1000
                call.status = status
            self.inflight.clear()

    # ---- turns --------------------------------------------------------
    def begin(self, text: str) -> None:
        with self.lock:
            self.turns.append(Turn("operator", text, time.time()))
            self.busy = True
            self.fault = ""
            self.step = "routing"
        self.publish()

    def landed(self, turn: Turn, *, wrote: str = "",
               confidence: float | None = None) -> None:
        self._settle("OK")
        with self.lock:
            self.turns.append(turn)
            self.busy = False
            self.step = ""
            self.spoke_at = time.time()
            self.spent += turn.cost
            self.n_turns += 1
            self.n_local += 0 if turn.model else 1
            self.last_ms = turn.ms
            self.last_model = turn.model
            if wrote:
                # Nothing is *held* — a capture lands in the note immediately,
                # by rule — so the status is `committed` and the confidence is
                # a prompt to correct it in a sentence, not a queue waiting on
                # an approval that never comes. `tui.Panes` says the same.
                self.writes.insert(0, (wrote, confidence if confidence
                                       is not None else 1.0, "committed",
                                       time.time()))
                del self.writes[6:]
        self.publish()

    def reset(self) -> None:
        """`/new`. The *conversation* ends, not the surface.

        So the transcript, the route and the writes attributed to "this
        session" all go — they belong to the recap V just filed — and the
        money does not. `spent` and the turn counts are what the budget gauge
        draws, and a gauge that zeroes whenever you change subject is a gauge
        that tells you the day was free. The uptime and session id in the
        footer are the HUD's own, and the HUD did not restart.
        """
        with self.lock:
            self.turns.clear()
            self.calls.clear()
            self.inflight.clear()
            self.writes.clear()
            self.fault = ""
            self.step = ""
        self.publish()

    def clear_log(self) -> None:
        """`/clear` — the screen, and nothing else. The conversation V is
        holding is untouched; this is the terminal's `\x1b[2J`, not `/new`."""
        with self.lock:
            self.turns.clear()
        self.publish()

    def failed(self, why: str) -> None:
        self._settle("ERR")
        with self.lock:
            self.busy = False
            self.fault = why
            self.step = ""
        self.publish()

    def live(self) -> dict:
        """The part of the HUD that must keep moving while the engine thread
        is blocked: the aperture, the route strip and the ticker."""
        now = time.time()
        name, detail = self.state(now)
        with self.lock:
            calls = [c.json() for c in self.calls]
            route = [c.json() for c in self.calls[:6]][::-1]
            turns = [t.json() for t in self.turns]
            writes = [{"path": p, "confidence": c, "status": s}
                      for p, c, s, _ in self.writes]
            session = {
                "id": f"s-{int(self.started) & 0xffff:04x}",
                "up": int(now - self.started),
                "turns": self.n_turns, "local": self.n_local,
                "spent": round(self.spent, 4), "writes": len(self.writes),
                "latency_ms": round(self.last_ms),
                "model": self.last_model,
            }
        return {"state": {"name": name, "detail": detail,
                          "tools": self.tools_lit()},
                "route": route, "ticker": calls, "turns": turns,
                "pending": writes, "session": session, "busy": session and
                self.busy}

    # ---- SSE ----------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self.lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self) -> None:
        """Wake every listener. A full-queue subscriber is dropped rather than
        waited on: a browser tab that has stopped reading is a tab that has
        been closed or backgrounded, and blocking a turn on it would let a
        dead socket stall the engine."""
        payload = self.live()
        with self.lock:
            self._seq += 1
            payload["seq"] = self._seq
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# --------------------------------------------------------------------------
# the snapshot — everything the panels draw, read live, on the engine thread


def _plain(text: str, limit: int = 120) -> str:
    """Markdown down to one line. The panels are single-line rows with an
    ellipsis; a wikilink rendered raw spends thirty of those characters on
    brackets and a path nobody can read at 12px."""
    import re
    out = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text or "")
    out = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).rsplit("/", 1)[-1],
                 out)
    out = re.sub(r"[*_`#]+", "", out).strip()
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _sender(raw: str) -> str:
    """`"Quera" <no-reply@quera.org>` down to `Quera`. The panel gives this
    78px; an address spends all of it on the domain."""
    who = (raw or "").split("<")[0].strip().strip('"')
    return _plain(who or (raw or "").strip("<>"), 28)


def _stem(rel: str) -> str:
    return rel[:-3] if rel.endswith(".md") else rel


def _events(engine, day: dt.date, now: dt.datetime) -> list[dict]:
    out = []
    for e in engine.q.calendar.on(day):
        at = e.starts_at()
        out.append({
            "time": e.time_label or ("all day" if e.all_day else ""),
            "title": _plain(e.summary, 90),
            "where": _plain(e.location or "", 50),
            "src": "CAL",
            "at": at.isoformat(timespec="minutes") if at else "",
            "past": bool(at and at < now),
            "kind": "cal",
        })
    for r in engine.reminders.pending():
        if r.due.date() != day:
            continue
        out.append({"time": r.due.strftime("%H:%M"), "title": _plain(r.text, 90),
                    "where": "", "src": "REM",
                    "at": r.due.isoformat(timespec="minutes"),
                    "past": r.due < now, "kind": "v"})
    out.sort(key=lambda e: (e["at"] or "99:99"))
    return out[:N_AGENDA]


def _tasks(engine, day: dt.date) -> list[dict]:
    out = []
    for t in engine.q.tasks.priorities(today=day, limit=N_TASKS):
        overdue = t.is_overdue(day)
        out.append({
            "id": t.id,
            "title": _plain(t.text, 90),
            # The design ranks P0/P1/P2. V ranks by overdue-then-due, which is
            # the same idea with the reason still attached, so the label is
            # derived from that rather than from a priority field the vault
            # does not have.
            "rank": "P0" if overdue else ("P1" if t.due else "P2"),
            "why": ("overdue" if overdue else
                    f"due {t.due.isoformat()}" if t.due else "no date"),
            "where": t.section or _stem(t.rel).rsplit("/", 1)[-1],
            "done": t.state == "done",
            "doing": t.state == "doing",
            "rel": t.rel,
        })
    return out


def _deadlines(engine, day: dt.date) -> list[dict]:
    out = []
    for r in engine.q.assignments(due_within=30).rows[:N_DEADLINES]:
        days = r.extra.get("due_in")
        if days is None:
            continue
        out.append({
            "title": _plain(str(r.get("title") or r.title), 70),
            "course": _plain(str(r.get("course") or ""), 60),
            "days": days,
            "left": "today" if days == 0 else
                    ("1d" if days == 1 else f"{days}d"),
            "status": str(r.get("status") or ""),
            "rel": r.rel,
        })
    return out


def _memories(engine) -> list[dict]:
    now = time.time()
    mems = engine.memory.all(active_only=True)
    mems.sort(key=lambda m: m.effective_strength(now), reverse=True)
    out = []
    for m in mems[:N_MEMS]:
        strength = m.effective_strength(now)
        age = m.age_days(now)
        seen = (f"used {m.used_count}×" if m.used_count else "never used")
        out.append({
            "id": m.id,
            # The design's STALE row is not a sixth kind — it is any memory
            # that has decayed past the point of being offered. Kept as a
            # display label so `memory.py`'s own vocabulary stays the four
            # kinds it actually stores.
            "kind": ("stale" if strength < 0.25 else m.kind).upper(),
            "text": _plain(m.text, 160),
            "why": _plain(m.why, 90),
            "seen": f"{seen} · {age:.0f}d old",
            "score": round(strength, 2),
            "agent": m.agent,
            "confidence": round(m.confidence, 2),
            "source": m.source,
        })
    return out


def _nudges(engine, day: dt.date) -> list[dict]:
    try:
        found = engine.nudges.all(today=day)
    except Exception:
        return []
    found.sort(key=lambda n: -n.urgency)
    return [{"kind": n.kind.replace("_", " ").upper(),
             "text": _plain(n.text, 140), "detail": _plain(n.detail, 120),
             "urgency": n.urgency}
            for n in found[:N_NUDGES]]


def _mail(engine, cache: dict) -> dict:
    """The one panel that is a network round trip, so it is the one panel with
    a cache in front of it. Not configured is a state the panel draws, not an
    error it hides: a blank INBOX and a disconnected one look identical, and
    only one of them is worth doing something about."""
    now = time.time()
    if cache.get("at", 0) + MAIL_TTL > now:
        return cache["val"]
    out: dict = {"state": "off", "items": []}
    try:
        from .gmail import Gmail
        g = Gmail(engine.s)
        if not g.configured():
            out["state"] = "unconfigured"
        elif not g.authorised():
            out["state"] = "unauthorised"
        else:
            # `search`, never `unseen`. `unseen()` marks what it returns as
            # seen in `gmail_seen.json`, which is how `scheduler.py` decides
            # what it has already pushed — a dashboard polling that would
            # quietly eat the queue and V would stop telling him about mail
            # he had not read. A panel that only draws must only read.
            msgs = g.search("in:inbox is:unread", limit=N_MAIL)
            out = {"state": "on", "account": g.account(),
                   "items": [{"from": _sender(m.from_),
                              "subject": _plain(m.subject or "(no subject)", 90),
                              "when": m.date.strftime("%H:%M") if m.date else "",
                              "unread": m.unread, "id": m.id}
                             for m in msgs]}
    except Exception as exc:
        out = {"state": "error", "items": [], "why": type(exc).__name__}
    cache["at"], cache["val"] = now, out
    return out


def _drift(engine, cache: dict) -> list[str]:
    now = time.time()
    if cache.get("at", 0) + DRIFT_TTL > now:
        return cache["val"]
    try:
        val = index.drift(engine.s, engine.conn)
    except Exception:
        val = []
    cache["at"], cache["val"] = now, val
    return val


def snapshot(engine, session: Session, caches: dict,
             now: dt.datetime | None = None) -> dict:
    """Every panel, in one call, on the engine thread.

    One call rather than one per panel for `glance()`'s reason and one more:
    six endpoints would each queue separately behind a running turn, so a
    single slow answer would arrive as six panels blanking one at a time
    instead of one snapshot arriving late.

    Free by construction. Everything here is SQL, Markdown and one cached
    Gmail read; nothing reaches a model, and `test_hud` asserts the cost.
    """
    now = now or dt.datetime.now()
    day = now.date()
    st = index.stats(engine.s)
    drift = _drift(engine, caches.setdefault("drift", {}))
    online = bool(engine.hermes.available and engine.s.shell_auto_escalate)
    mem_stats = engine.memory.stats()
    deadlines = _deadlines(engine, day)
    live = session.live()

    # The design's `alarmMode` is a prop you flip. Here it is a fact: the edge
    # pulses when something is actually due inside a day, and goes quiet by
    # itself when it is not.
    soonest = min((d["days"] for d in deadlines), default=None)
    # `T-6H · HW4` in the mockup. Built from the real deadline, and *not*
    # upper-cased or cut to a character budget: half this vault's course names
    # are Persian, where `.upper()` is a no-op and an 18-character cut lands
    # mid-word. The chip ellipsises in CSS instead, which is right-to-left
    # aware in a way `str[:n]` is not.
    alarm = {"on": soonest is not None and soonest <= 1,
             "label": (f"{deadlines[0]['left']} · "
                       f"{deadlines[0]['title'] or deadlines[0]['course']}"
                       if deadlines else "")}

    cap = float(getattr(engine.s, "daily_budget_usd", 2.0)) or 2.0
    return {
        "at": now.isoformat(timespec="seconds"),
        "day": day.isoformat(),
        "agent": engine.persona.key,
        "agents": [{"key": p.key,
                    "allowed": p.allows(persona_mod.HUD),
                    "where": ", ".join(p.surfaces)}
                   for p in persona_mod.PERSONAS.values()],
        "alarm": alarm,
        "header": {
            "model": (engine.s.shell_provider if online else "local"),
            "online": online,
            "memories": mem_stats.get("active", 0),
        },
        "gauges": {
            "budget": {"used": live["session"]["spent"], "cap": cap},
            "latency": {"ms": live["session"]["latency_ms"]},
            "context": {"turns": live["session"]["turns"],
                        "local": live["session"]["local"]},
            "drift": {"files": len(drift), "notes": st["notes"],
                      "sample": [_stem(r).rsplit("/", 1)[-1] for r in drift[:3]]},
        },
        "vault": {
            "notes": st["notes"], "words": st["words"], "chunks": st["chunks"],
            "types": len(st["types"]), "tasks_open": st["tasks_open"],
            "fresh": not drift,
            "indexed": int(index.last_indexed(engine.conn)),
            "network": "local-first" if not online else "escalation on",
        },
        "day_events": _events(engine, day, now),
        "tasks": _tasks(engine, day),
        "deadlines": deadlines,
        "mems": _memories(engine),
        "nudges": _nudges(engine, day),
        "mail": _mail(engine, caches.setdefault("mail", {})),
    }


# --------------------------------------------------------------------------
# the engine, on one thread


class HudError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _Bridge:
    """One engine, one thread, a queue in front of it.

    `httpapi._Bridge` is the same shape and its docstring carries the full
    argument. Copied rather than imported for two reasons that would both have
    to be undone to share it: that one constructs its engine with
    `surface=PHONE`, and it owns the phone's transcriber, synthesizer and
    speech spools, none of which this surface has. What is worth sharing is
    the *reasoning*, and that is in both docstrings.
    """

    def __init__(self, s: Settings, session: Session):
        self.s = s
        self.session = session
        self._in: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._boot_error: BaseException | None = None
        self._engine: Engine | None = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="v-hud-engine")
        self._thread.start()

    def _run(self) -> None:
        import atexit
        try:
            engine = Engine(self.s, surface=persona_mod.HUD)
            engine.start_session()
        except BaseException as exc:        # noqa: BLE001 — reported to callers
            self._boot_error = exc
            self._ready.set()
            return
        # The aperture's arcs, the route strip and the ticker all come from
        # here. Set for the life of the server: there is no spinner on this
        # surface to hand it back to.
        engine.progress = self.session.progress
        atexit.register(self._safe_end, engine)
        self._engine = engine
        self._ready.set()
        try:
            while True:
                fn, box, done = self._in.get()
                if fn is None:
                    break
                try:
                    box["val"] = fn(engine)
                except BaseException as exc:    # noqa: BLE001 — to the caller
                    box["err"] = exc
                finally:
                    done.set()
        finally:
            self._safe_end(engine)
            engine.close()

    @staticmethod
    def _safe_end(engine: Engine) -> None:
        try:
            engine.end_session("interrupted")
        except Exception:
            pass

    def call(self, fn, timeout: float = SNAPSHOT_TIMEOUT):
        self._ready.wait(120)
        if self._boot_error is not None:
            raise HudError(503, f"engine failed to start: {self._boot_error}")
        box: dict = {}
        done = threading.Event()
        self._in.put((fn, box, done))
        if not done.wait(timeout):
            raise HudError(504, "V is still working on the previous message")
        if "err" in box:
            err = box["err"]
            if isinstance(err, HudError):
                raise err
            raise HudError(500, f"{type(err).__name__}: {err}")
        return box["val"]

    def stop(self) -> None:
        self._in.put((None, {}, threading.Event()))


class Hud:
    """The application. Owns the engine thread, the session and the caches."""

    def __init__(self, s: Settings | None = None):
        self.s = s or default_settings
        self.session = Session()
        self.bridge = _Bridge(self.s, self.session)
        self.caches: dict = {}
        self._snap: dict | None = None
        self._snap_at = 0.0
        self._snap_lock = threading.Lock()

    # ---- reads --------------------------------------------------------
    def snapshot(self, *, force: bool = False) -> dict:
        """The panels. Serves the previous one if the engine thread is busy.

        This is the phone widget's contract applied to a screen: a snapshot
        that arrives late and stamped is better than a spinner, and much
        better than six panels that blank while a model thinks. `stale` is on
        the payload so the page can dim rather than pretend.
        """
        if not force and self.session.busy and self._snap is not None:
            return {**self._snap, "stale": True, "live": self.session.live()}
        try:
            snap = self.bridge.call(
                lambda e: snapshot(e, self.session, self.caches))
        except HudError:
            if self._snap is None:
                raise
            return {**self._snap, "stale": True, "live": self.session.live()}
        with self._snap_lock:
            self._snap, self._snap_at = snap, time.time()
        return {**snap, "stale": False, "live": self.session.live()}

    def search(self, text: str) -> dict:
        """The VAULT surface. Semantic when the embeddings are built, keyword
        when they are not — and it says which, because "0.87" and "matched the
        word" are not the same claim and the design shows a score for both."""
        def work(engine):
            text_ = text.strip()
            if not text_:
                return {"backend": "", "hits": []}
            if engine.rag.ready:
                r = engine.rag.search(text_, k=N_HITS)
                seen, hits = set(), []
                for h in r.hits:
                    if h.rel in seen:
                        continue
                    seen.add(h.rel)
                    hits.append({"score": round(h.score, 2),
                                 "title": _stem(h.rel).rsplit("/", 1)[-1],
                                 "path": h.rel, "why": h.source,
                                 "snippet": _plain(h.text, 160),
                                 "heading": h.heading})
                return {"backend": r.backend or "semantic", "ms": round(r.ms),
                        "hits": hits}
            res = engine.q.search(text_, limit=N_HITS)
            return {"backend": "keyword", "ms": 0,
                    "hits": [{"score": None,
                              "title": r.title,
                              "path": r.rel, "why": "keyword",
                              "snippet": _plain(r.extra.get("snippet", ""), 160),
                              "heading": ""}
                             for r in res.rows]}
        return self.bridge.call(work)

    # ---- writes -------------------------------------------------------
    def chat(self, text: str) -> dict:
        """One turn. The session is updated on the way in and on the way out
        so the aperture moves while the engine thread is busy."""
        self.session.begin(text)
        try:
            resp = self.bridge.call(lambda e: e.handle(text),
                                    timeout=CHAT_TIMEOUT)
        except HudError as exc:
            self.session.failed(exc.message)
            raise
        except Exception as exc:                       # noqa: BLE001
            self.session.failed(f"{type(exc).__name__}: {exc}")
            raise
        return self._land(resp)

    def _land(self, resp) -> dict:
        """A `Response` onto the transcript. Shared by chat and the commands
        that are engine methods, so a `/brief` renders with the same cost line
        an answer does — because it *is* one, and hiding that would make the
        cheapest surface look like the free one."""
        data = resp.data or {}
        turn = Turn(
            role="v", text=resp.render(), at=time.time(),
            route=data.get("model") and
            f"{resp.route or 'shell'}→{data['model']}" or resp.route,
            cites=list(resp.citations)[:6],
            cost=float(data.get("list_cost_usd") or 0.0),
            ms=resp.ms, model=str(data.get("model") or ""),
            confidence=data.get("confidence"),
            state=_voice_state(resp),
        )
        self.session.landed(turn, wrote=str(data.get("target") or "")
                            if data.get("commit") else "",
                            confidence=data.get("confidence"))
        return {"turn": turn.json(), "live": self.session.live()}

    def _say(self, text: str, *, state: str = "speaking",
             route: str = "local") -> dict:
        """A line from the surface itself rather than from the engine. `$0`,
        and it says so — these are the commands that never reach a model."""
        turn = Turn(role="v", text=text, at=time.time(), route=route,
                    state=state)
        self.session.landed(turn)
        return {"turn": turn.json(), "live": self.session.live()}

    def command(self, line: str) -> dict:
        """A slash command, run for real instead of sent to a model.

        Without this the HUD had no notion of commands at all: `/new` went to
        the router as a sentence, Sonnet said "Ready, sir.", it cost $0.10 and
        the session it was supposed to end ran on. Everything below is either
        an `Engine` method — the same one `cli._repl_command` calls — or a
        surface concern the browser owns and never reaches here.

        `/why`, `/help`, `/clear` and `/copy` are handled in the page: the
        trace is already on screen, the palette is a keystroke, and the
        clipboard belongs to the browser.
        """
        parsed = commands_mod.split(line)
        if parsed is None:
            raise HudError(400, "not a command")
        cmd, rest = parsed
        known = {c[0] for c in commands_mod.COMMANDS}

        if cmd in ("new", "quit", "exit"):
            # The escape hatch for the cost of remembering: a session that has
            # run all day re-sends all day on every turn. It files what you
            # are leaving rather than discarding it — same as the REPL, which
            # is why it is `end_session` and not a list being cleared.
            def work(engine):
                engine.end_session("new")
                engine.start_session()
                return True
            self.bridge.call(work, timeout=120)
            self.session.reset()
            return self._say("Fresh thread — nothing before this point "
                             "carries over.")

        if cmd == "undo":
            return self._land(self.bridge.call(
                lambda e: e.undo(rest or None), timeout=120))
        if cmd == "status":
            return self._land(self.bridge.call(lambda e: e.status(), timeout=120))
        if cmd == "brief":
            return self._land(self.bridge.call(lambda e: e.brief(),
                                               timeout=CHAT_TIMEOUT))
        if cmd == "evening":
            return self._land(self.bridge.call(lambda e: e.evening(),
                                               timeout=CHAT_TIMEOUT))
        if cmd == "nudges":
            return self._land(self.bridge.call(lambda e: e.nudge_digest(),
                                               timeout=120))
        if cmd in ("tasks", "mem"):
            # Both are model turns in the REPL and stay model turns here; the
            # point of the shortcut is the phrasing, not a different engine.
            said = ("what should I do today" if cmd == "tasks"
                    else (f"what do you know about {rest}" if rest
                          else "what do you remember"))
            return self.chat(said)

        if cmd == "quiet":
            def work(engine):
                hour = engine.s.quiet_hours[1]
                until = (dt.datetime.now() + dt.timedelta(days=1)).replace(
                    hour=hour, minute=0, second=0, microsecond=0)
                commands_mod.save_prefs(
                    engine.s.state_dir,
                    quiet_until=until.isoformat(timespec="seconds"))
                return until
            until = self.bridge.call(work, timeout=60)
            return self._say(f"Nothing unprompted until {until:%a %H:%M}. "
                             f"Reminders you set yourself still fire.")

        if cmd == "local":
            def work(engine):
                on = not commands_mod.prefs(engine.s.state_dir).get("local_only")
                commands_mod.save_prefs(engine.s.state_dir, local_only=on)
                engine.s.shell_auto_escalate = not on
                engine.router.agentic = not on
                return on
            on = self.bridge.call(work, timeout=60)
            self.caches.pop("state", None)
            return self._say("Local only — nothing will leave this machine."
                             if on else "Escalation back on.")

        if cmd == "clear":
            self.session.clear_log()
            return {"turn": None, "live": self.session.live()}

        if cmd == "cost":
            live = self.session.live()["session"]
            cap = float(getattr(self.s, "daily_budget_usd", 2.0)) or 2.0
            return self._say(
                f"**${live['spent']:.4f}** this session over {live['turns']} "
                f"turn{'s' if live['turns'] != 1 else ''}, "
                f"{live['local']} of them free. Cap is ${cap:.2f} a day.\n\n"
                f"The gauges on the left are the same numbers, live.")

        if f"/{cmd}" in known:
            # In the table, handled in the page. Reaching here means the
            # client sent something it should have kept.
            raise HudError(400, f"/{cmd} is handled in the page")
        return self._say(f"Unknown command `/{cmd}` — press `/` for the list.",
                         state="refused", route="—")

    def task(self, task_id: int, state: str) -> dict:
        """The design's checkbox, wired to the real one.

        Goes through `tasks.complete` — which rewrites the checkbox in the
        note, commits it per-path as `[bot]` and is revertible with `v undo` —
        rather than flipping a row in the index, because the Markdown is the
        source of truth and an index the vault disagrees with is the bug this
        whole package is built to avoid.
        """
        def work(engine):
            from . import tasks as tasks_mod
            valid = {"done": tasks_mod.DONE, "open": tasks_mod.OPEN,
                     "doing": tasks_mod.DOING, "dropped": tasks_mod.DROPPED}
            if state not in valid:
                raise HudError(400, f"unknown state {state!r}")
            t = engine.q.tasks.get(task_id)
            if t is None:
                raise HudError(404, f"no task {task_id}")
            if t.state == valid[state]:
                return {"ok": True, "unchanged": True}
            engine.q.tasks.complete(t, engine.vault, state=valid[state],
                                    today=dt.date.today())
            return {"ok": True, "unchanged": False}
        out = self.bridge.call(work, timeout=60)
        self.caches.pop("drift", None)     # the note just moved under the index
        return out

    def agent(self, key: str) -> dict:
        """The design's palette switch — jarvis · friday · edith — which in
        this system is not a colour scheme but which agent answers. EDITH is
        refused here and says so; see the module docstring."""
        def work(engine):
            ok, msg = engine.switch_agent(key)
            return {"ok": ok, "message": msg, "agent": engine.persona.key}
        return self.bridge.call(work, timeout=60)

    def stop(self) -> None:
        self.bridge.stop()


def _voice_state(resp) -> str:
    """Which of the four voice colours this answer is drawn in. `ui.py` owns
    the colours; this only picks the name, and the rule it encodes is the
    brief's: a refusal is never styled as an error."""
    data = resp.data or {}
    if data.get("refused") or resp.intent == "refused":
        return "refused"
    conf = data.get("confidence")
    if conf is not None and conf < 0.5:
        return "unsure"
    if not data.get("model"):
        return "degraded" if data.get("degraded") else "speaking"
    return "speaking"


# --------------------------------------------------------------------------
# the wire


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "v-hud"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    app: Hud                       # set by `_make_server`

    # ---- plumbing -----------------------------------------------------
    def log_message(self, fmt, *args):
        """Silent by default. A HUD polls twice a second; a request log of
        that is a wall of text over whatever else the terminal was doing."""
        if self.server.verbose:                     # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page talks to nothing but itself. A CSP that says so is one
        # line and it is what makes "a browser tab" an acceptable place to
        # render a vault: no font CDN, no analytics, no exfiltration path for
        # a note that ends up in the DOM.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; "
                         "font-src 'self'; object-src 'none'; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False,
                                      default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, exc: HudError) -> None:
        self._json({"error": exc.message}, status=exc.status)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _guard(self) -> bool:
        """Loopback, and a browser that is actually on this page.

        The bind is already `127.0.0.1`, so the first check is belt-and-braces.
        The second is not: any page in any tab can POST a form to
        `http://127.0.0.1:7717/api/chat` without reading the response, and
        that is enough to make V answer — and write. `Sec-Fetch-Site` is sent
        by every browser that can run this page and cannot be forged from
        script, so a cross-site POST is refused before it reaches the engine.
        """
        host = (self.client_address[0] or "")
        if host not in ("127.0.0.1", "::1", "localhost"):
            self._json({"error": "loopback only"}, status=403)
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            self._json({"error": "cross-site request refused"}, status=403)
            return False
        return True

    # ---- routes -------------------------------------------------------
    def do_GET(self) -> None:                       # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        try:
            if path in ("/", "/index.html"):
                return self._asset("index.html")
            if path == "/api/state":
                return self._json(self.app.snapshot(force="force=1" in query))
            if path == "/api/live":
                return self._json(self.app.session.live())
            if path == "/api/commands":
                # Static, so it is fetched once at boot rather than riding on
                # every four-second poll.
                return self._json({"commands": [
                    {"cmd": c, "what": what, "said": said}
                    for c, what, said in commands_mod.COMMANDS
                    if c not in commands_mod.NOT_ON_HUD]})
            if path == "/api/search":
                from urllib.parse import parse_qs, unquote
                q = (parse_qs(query).get("q") or [""])[0]
                return self._json(self.app.search(unquote(q)))
            if path == "/api/events":
                return self._events()
            if "/" not in path[1:] and path.endswith((".css", ".js")):
                return self._asset(path[1:])
        except HudError as exc:
            return self._fail(exc)
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:                      # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        body = self._body()
        try:
            if path == "/api/chat":
                text = (body.get("text") or "").strip()
                if not text:
                    raise HudError(400, "text is required")
                return self._json(self.app.chat(text))
            if path == "/api/task":
                return self._json(self.app.task(int(body.get("id") or 0),
                                                body.get("state") or "done"))
            if path == "/api/command":
                text = (body.get("text") or "").strip()
                if not text.startswith("/"):
                    raise HudError(400, "not a command")
                return self._json(self.app.command(text))
            if path == "/api/agent":
                return self._json(self.app.agent(body.get("key") or "v"))
            if path == "/api/typing":
                # The one state the browser knows and the server cannot: a
                # keystroke. It is a timestamp, not a transcript — nothing
                # typed is sent until Enter.
                self.app.session.typing = time.time()
                self.app.session.publish()
                return self._json({"ok": True})
        except HudError as exc:
            return self._fail(exc)
        except Exception as exc:                    # noqa: BLE001
            return self._fail(HudError(500, f"{type(exc).__name__}: {exc}"))
        self._json({"error": "not found"}, status=404)

    # ---- assets -------------------------------------------------------
    def _asset(self, name: str) -> None:
        path = (ASSETS / name).resolve()
        if not str(path).startswith(str(ASSETS.resolve())) or not path.is_file():
            return self._json({"error": "not found"}, status=404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    # ---- SSE ----------------------------------------------------------
    def _events(self) -> None:
        """The live half — aperture, route, ticker — pushed rather than polled.

        Polling would work for the panels and does; it cannot work for these,
        because during a turn the engine thread is blocked and a poll queues
        behind it. This stream is fed by `Session`, which is in front of the
        bridge, so it keeps moving through the slowest answer.
        """
        q = self.app.session.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._push(self.app.session.live())
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    # A comment frame. Keeps the socket open through a proxy
                    # and, more usefully, is how this loop notices the tab was
                    # closed — the write raises and we unsubscribe.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._push(payload)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            self.app.session.unsubscribe(q)

    def _push(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False


def _make_server(app: Hud, port: int, *, verbose: bool = False) -> _Server:
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    srv = _Server(("127.0.0.1", port), handler)
    srv.verbose = verbose
    return srv


def free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    """The first free port at or after `start`.

    A HUD is a thing you leave open and start again after a crash, and
    `SO_REUSEADDR` does not save you from a copy that is genuinely still
    running. Refusing to start is the wrong answer for a surface whose whole
    job is to be glanced at.
    """
    for n in range(start, start + tries):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", n))
                return n
            except OSError:
                continue
    raise HudError(500, f"no free port in {start}–{start + tries}")


def serve(settings: Settings | None = None, *, port: int | None = None,
          open_browser: bool = True, verbose: bool = False,
          on_ready=None) -> int:
    """Run the HUD until Ctrl-C. Returns a process exit code."""
    s = settings or default_settings
    app = Hud(s)
    chosen = port if port is not None else free_port()
    srv = _make_server(app, chosen, verbose=verbose)
    url = f"http://127.0.0.1:{chosen}/"
    if on_ready:
        on_ready(url, srv)
    if open_browser:
        # Threaded because `webbrowser.open` blocks for as long as the browser
        # takes to come up, and the page it opens needs this server already
        # answering.
        threading.Thread(target=_open_later, args=(url,), daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        app.stop()
    return 0


def _open_later(url: str, delay: float = 0.4) -> None:
    import webbrowser
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        pass
