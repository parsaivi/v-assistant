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

**And it is the surface with room for a picture.** Every other one V has is
words: the terminal cannot draw a Neshan raster at the width a transcript can
spare, and `render.py` has said so since the half-blocks were tried and
removed. So a chart went to `webbrowser.open()` and a screenshot pasted into a
note was invisible unless Obsidian was the window in front. `Frame` and the
viewport are the fix — see `media.py` for the finding, which is the half with
the interesting argument in it. Two things are worth stating here: the
pictures arrive on `_Scout`'s thread and never on the one answering, so a
static-map fetch is never latency added to an answer in order to decorate it;
and the bytes are addressed by a hash of a path the session has already
decided to show, so `/api/media` has no filename in it to traverse with.

```
browser ──HTTP(loopback)──►  _Handler ──►  _Bridge ──►  Engine.handle
   ▲                             │                          │
   │                             ▼                          ▼
   ├── SSE /api/events ◄──── Session ◄──── engine.progress (tool events)
   │                             ▲                          │
   │                          frames                   rel · rels
   │                             │                          ▼
   └── GET /api/media ◄──────────┴──────── _Scout ◄──── notes · places
                                              ▲
                                        media.Spool ◄── render.save()
                                                        (any process)
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
import hashlib
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

from . import (commands as commands_mod, index, jalali as jalali_mod,
               media as media_mod, notes as notes_mod,
               persona as persona_mod, weekplan)
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
#: The TASKS *view* is not the TASKS card. The card is the six most urgent
#: things and fits beside the conversation; the view is the whole backlog,
#: which is the only place a task can be got rid of. Capped anyway — a list
#: nobody can reach the bottom of is a list nobody prunes, and the count above
#: it stays honest about what was cut.
N_TASK_VIEW = 120
N_DEADLINES = 4
N_MEMS = 8
N_NUDGES = 4
#: The silence list is a *reminder* that it exists, not a management screen —
#: it sits under the panel it censors, so it has to stay short.
N_NUDGES_OFF = 6
N_MAIL = 4
N_HITS = 8
#: The tool-bus ticker and the sparkline behind it.
N_TICKER = 24
#: How many pictures the viewport keeps. A turn that reads six bug-report
#: notes can produce two dozen screenshots, and a filmstrip nobody can reach
#: the end of is a filmstrip nobody looks at. Newest wins; the note path is on
#: every frame, so what falls off is one click away in Obsidian.
N_FRAMES = 12
#: How many notes one turn may be scouted for pictures at all. The cap is on
#: the *turn*, not the call: `vault_search` alone can name twelve notes.
N_SCOUT = 10
#: Of those, how many may come from a *result set* rather than from a call's
#: own argument — see `_Scout` for why the two are not the same signal.
N_LOOSE = 3
#: How many place maps one turn may fetch. Each one is a Neshan request
#: against a quota, so this is a cost cap as much as a design one: no
#: question is about four places at once.
N_MAPS = 3
#: How often the scout looks at the render spool. Fast enough that a chart
#: appears while V is still writing the sentence announcing it.
SPOOL_POLL = 0.5

#: Gmail is the one panel that is not free — it is a network round trip to
#: Google. So it is cached, and the HUD redraws from the cache rather than
#: asking again on every poll.
MAIL_TTL = 300.0
#: The drift walk is one `stat` per note. Cheap, but not free at 3,400 notes
#: times a two-second poll.
DRIFT_TTL = 30.0
#: Whether today is a holiday is a fact that changes once a day, so this is
#: long. Only the *flag* is behind it — the Jalali date beside the clock is
#: arithmetic and is recomputed every poll, which is why the date never
#: blanks when the calendar service is unreachable.
JALALI_TTL = 3600.0 * 6

#: The PLACES view's basemap. Neshan has no tile endpoint a service key can
#: reach — the SDK's tiles are behind the *web* key and a CDN script — so the
#: map is tiled out of the static endpoint instead: one image per cell of a
#: fixed pixel grid, centred on the cell. `TILE_PAD` is fetched on every side
#: and cropped away by the page, because every static image carries Neshan's
#: logo in its corner and a grid of them is a wall of logos. The attribution
#: is drawn once, on the map, instead.
TILE_PX = 512
TILE_PAD = 128
#: 10 is the whole of Tehran on one screen; past 17 the static raster has
#: nothing more to say. Neshan itself accepts 5–19.
TILE_ZOOMS = (10, 17)
#: New tiles fetched per rolling hour. A tile is cached on disk for good —
#: streets move slower than this process restarts — so this only bites the
#: first time a part of the city is looked at closely. It exists because each
#: one is a request against the same Neshan quota `N_MAPS` guards, and a
#: dashboard that can spend a quota by being dragged around is one drag away
#: from spending all of it.
TILE_BUDGET = 60
#: A tile that failed is not asked for again for this long. A pan back over a
#: dead cell would otherwise be a request every time; the page retries a hole
#: on its own once this has passed.
TILE_RETRY = 60.0
#: The anchor every distance on the map is measured from — the same name
#: `vault_places_search(near=…)` resolves, so the map and V agree on it.
PLACE_HOME = "Home"

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
class Frame:
    """One picture the viewport can show, and where it came from.

    A frame is a *view*, not a delivery: the same PNG can be on this screen,
    on the phone and in a Telegram chat at once, and showing it twice costs
    nothing. That is the whole reason `media.Spool` exists beside the outbox
    in `files.py` rather than reusing it — see that module's docstring.

    `id` is a hash of the path rather than the path itself. Not obscurity:
    `/api/media?id=…` then has no string a caller controls anywhere near a
    filesystem, so path traversal is not defended against, it is unexpressible.
    The registry that maps it back is `Session.media`.
    """

    id: str
    kind: str                  # image · page
    title: str
    caption: str = ""
    source: str = "note"       # note · render · place
    rel: str = ""
    bytes: int = 0
    at: float = field(default_factory=time.time)

    def json(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "caption": self.caption, "source": self.source,
                "rel": self.rel, "bytes": self.bytes,
                "time": dt.datetime.fromtimestamp(self.at).strftime("%H:%M:%S")}


def frame_id(path) -> str:
    """A stable, short handle for a file on disk. Stable matters: the same
    screenshot cited by two notes in one turn is one frame, not two."""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


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
        # The viewport. `frames` is newest-first like `calls`; `media` is the
        # id→path registry behind `/api/media` and outlives the frame list on
        # purpose — a browser still fetching a picture that has just been
        # pushed off the filmstrip should get it, not a 404.
        self.frames: list[Frame] = []
        self.media: dict[str, Path] = {}
        #: Set by `Hud` to `_Scout.saw`. None in every test that builds a
        #: bare Session, which is why `_progress` checks rather than assumes.
        self.watch = None
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
            self._look(ev)
        elif ev.kind == "tool_result":
            # The other half of the same picture. `rel` says what a call was
            # *about* as it goes out; `rels` says what it came back holding,
            # which for `vault_search` is eight notes at once and is where
            # most of the screenshots come from.
            self._look(ev)
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

    def _look(self, ev) -> None:
        """Hand the notes a call named to whoever is finding pictures.

        The two fields go over separately and stay separate the whole way,
        because they are not the same claim and `shells.py` says so: `rel` is
        the note a call names in its own argument — "it is reading *this*" —
        while `rels` is everything scraped out of what came back, "a
        constellation". `_Scout` spends them differently.

        Handed off rather than resolved here for the reason this whole class
        exists: `_progress` runs on the thread running the turn, and a note
        read — never mind a static-map fetch — on that thread is latency
        added to the answer in order to decorate it.
        """
        if self.watch is None:
            return
        chosen = [r for r in [getattr(ev, "rel", "")] if r]
        listed = [r for r in (getattr(ev, "rels", []) or []) if r]
        if chosen or listed:
            self.watch(chosen, listed)

    # ---- the viewport --------------------------------------------------
    def show(self, items) -> None:
        """Put pictures on screen. Newest batch first, in the order the note
        embeds them, deduplicated by file.

        Both halves of that ordering are deliberate. *Newest batch first*
        because the picture V has just found is the one being talked about.
        *Document order within the batch* because a bug-report note's four
        screenshots are a sequence — inserting each one at the front turned
        them back to front, so the panel opened on the last screenshot of the
        note and the filmstrip read right to left.

        Deduplicated because the same screenshot really does arrive twice in
        one turn — `vault_search` names the note, then `vault_read` reads it —
        and a filmstrip with the same frame in it twice reads as a bug in the
        surface rather than as two mentions of one picture.
        """
        # Built before the lock is taken: `Item.bytes` is a `stat()`, and the
        # SSE loop is waiting on this lock to draw the aperture.
        made = [(frame_id(i.path), i, i.bytes) for i in items]
        new = []
        with self.lock:
            have = {f.id for f in self.frames}
            for fid, item, size in made:
                if fid in have:
                    continue
                have.add(fid)
                new.append(Frame(id=fid, kind=item.kind, title=item.title,
                                 caption=item.caption, source=item.source,
                                 rel=item.rel, bytes=size, at=item.at))
                self.media[fid] = Path(item.path)
            self.frames[:0] = new
            del self.frames[N_FRAMES:]
            # The registry outlives the strip, but not forever.
            if len(self.media) > 200:
                for key in list(self.media)[:-200]:
                    del self.media[key]
        if new:
            self.publish()

    def clear_frames(self) -> None:
        with self.lock:
            self.frames.clear()
        self.publish()

    def media_path(self, fid: str) -> Path | None:
        with self.lock:
            return self.media.get(fid)

    # ---- turns --------------------------------------------------------
    def begin(self, text: str) -> None:
        with self.lock:
            self.turns.append(Turn("operator", text, time.time()))
            self.busy = True
            self.fault = ""
            self.step = "routing"
        # The frames stay — a follow-up question is usually *about* what is
        # on screen — but the scout forgets which notes it has already been
        # through, so asking again about the same note shows it again.
        if self.watch is not None:
            self.watch(None, None)
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
        """`/new`. Every counter on this object is scoped to one conversation,
        so all of them go.

        This used to keep `spent` and the turn counts across a reset, on the
        argument that a budget gauge which zeroes whenever you change subject
        tells you the day was free. The argument was right and the fix was in
        the wrong place: the money was being read off a *session* counter and
        drawn against a *daily* cap, which is a number that was never the
        day's — it also zeroed on every restart, and counted only turns taken
        at this surface while Telegram spent from the same wallet unrecorded.
        `spend_today()` answers that question properly now, off the run log
        every surface already writes, so this object no longer has to
        half-answer it. What is left here is the session, and after `/new`
        there is a new one — new id, uptime from zero, nothing carried.
        """
        with self.lock:
            self.turns.clear()
            self.calls.clear()
            self.inflight.clear()
            self.writes.clear()
            self.frames.clear()
            self.fault = ""
            self.step = ""
            self.spent = 0.0
            self.n_turns = 0
            self.n_local = 0
            self.last_ms = 0.0
            self.last_model = ""
            self.spoke_at = 0.0
            # The footer's `s-xxxx · 12m`. It reads as the conversation's age
            # — that is what a person sitting in front of it takes it for —
            # so after `/new` it has to be the new conversation's age, not the
            # tab's.
            self.started = time.time()
        self.publish()

    def clear_log(self) -> None:
        """`/clear` — the screen, and nothing else. The conversation V is
        holding is untouched; this is the terminal's `\x1b[2J`, not `/new`.

        The viewport is part of the screen, so it goes too."""
        with self.lock:
            self.turns.clear()
            self.frames.clear()
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
            frames = [f.json() for f in self.frames]
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
                "pending": writes, "frames": frames, "session": session,
                "busy": session and self.busy}

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


def _lanes(blocks: list[dict]) -> None:
    """Lay overlapping blocks out, in place. Two rules, and the split matters.

    **Contained blocks nest.** A 30-minute standup inside an eight-hour work
    block is not two things competing for the column — it is one thing that
    happens *during* another. Splitting the column for it wastes half the day's
    width on a stripe of empty work block, which is what "beside" looked like
    in practice. So a block wholly inside a longer one gets a `depth`: it is
    drawn on top, inset from the left, and — because it is on top — it is what
    the pointer hits. Clicking the small thing you aimed at is the whole point.

    **Genuine partial overlaps split.** 13:00–14:00 against 13:30–15:00 is two
    things neither of which contains the other, and there is no honest way to
    draw that except side by side. Those get lanes, assigned only among peers
    at the same depth so a nested block never widens its parent's cluster.
    """
    for blk in blocks:
        span = blk["e"] - blk["s"]
        blk["depth"] = sum(
            1 for other in blocks
            if other is not blk
            and other["s"] <= blk["s"] and blk["e"] <= other["e"]
            and (other["e"] - other["s"]) > span)

    def assign(cluster: list[dict]) -> None:
        ends: list[int] = []
        for blk in cluster:
            for i, end in enumerate(ends):
                if blk["s"] >= end:
                    blk["lane"], ends[i] = i, blk["e"]
                    break
            else:
                blk["lane"] = len(ends)
                ends.append(blk["e"])
        for blk in cluster:
            blk["lanes"] = len(ends)

    by_depth: dict[int, list[dict]] = {}
    for blk in blocks:
        by_depth.setdefault(blk["depth"], []).append(blk)
    for peers in by_depth.values():
        peers.sort(key=lambda b: (b["s"], -(b["e"] - b["s"])))
        cluster: list[dict] = []
        reach = 0
        for blk in peers:
            if cluster and blk["s"] >= reach:
                assign(cluster)
                cluster, reach = [], 0
            cluster.append(blk)
            reach = max(reach, blk["e"])
        if cluster:
            assign(cluster)


def _week(engine, day: dt.date, now: dt.datetime) -> dict:
    """The week as a grid: seven columns of blocks over a full 24-hour canvas.

    Three things here exist only because a screen needs them and the CLI does
    not: minute offsets, so the page lays a block out by arithmetic instead of
    parsing times in a zone it disagrees with V about; lanes, so two things at
    the same hour sit beside each other instead of on top of each other; and
    `line`, the block's own text, which is both its identity and exactly what
    `vault.edit` needs back to change it.

    What is deliberately *not* here any more is free time. It was a chip under
    every day header reading "11h free", and a calendar that tallies the hours
    you have not committed is nagging rather than reporting. Free time is an
    answer to a question — `v week --free`, `vault_free_time` — and it stays
    there.

    Free by construction, like every other panel: two Markdown files read and
    some arithmetic. Nothing reaches a model.
    """
    wp = engine.q.weekplan
    start = weekplan.week_start_of(day)
    today = now.date()
    minute_now = now.hour * 60 + now.minute

    days, earliest, latest = [], None, None
    claimed = 0
    load: dict[str, int] = {}
    for i in range(7):
        d = start + dt.timedelta(days=i)
        is_today = d == today
        blocks = []
        for b in wp.busy(d):
            if not b.end:
                continue
            s = b.start.hour * 60 + b.start.minute
            e = b.end.hour * 60 + b.end.minute
            earliest = s if earliest is None else min(earliest, s)
            latest = e if latest is None else max(latest, e)
            claimed += b.minutes
            load[b.kind] = load.get(b.kind, 0) + b.minutes
            blocks.append({
                # The written line, which is the block's identity: the HUD
                # hands it straight back to delete or move the thing, and
                # `vault.edit` refuses it if the week has changed underneath.
                "line": b.line(),
                "kind": b.kind,
                "title": _plain(b.label or b.kind, 80),
                "label": _plain(b.label, 80),
                "where": _plain(b.location, 40),
                "time": b.time_label,
                "from": b.start.strftime("%H:%M"),
                "to": b.end.strftime("%H:%M"),
                "s": s, "e": e, "mins": b.minutes,
                "lane": 0, "lanes": 1, "depth": 0,
                "now": is_today and s <= minute_now < e,
                "past": d < today or (is_today and e <= minute_now),
            })
        _lanes(blocks)
        days.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %-d").upper(),
            "weekday": d.strftime("%A"),
            "today": is_today,
            "blocks": blocks,
        })

    rel = wp.week_rel(start)
    current = wp.now_block(now)
    nxt = None
    for d in days:
        for blk in d["blocks"]:
            if d["date"] > today.isoformat() or (
                    d["date"] == today.isoformat() and blk["s"] > minute_now):
                nxt = {"date": d["date"], "day": d["label"],
                       "today": d["date"] == today.isoformat(),
                       "at": blk["from"], "kind": blk["kind"],
                       "title": blk["title"]}
                break
        if nxt:
            break

    return {
        "start": start.isoformat(),
        "label": weekplan.week_label(start),
        "rel": rel,
        "exists": (engine.s.vault / rel).exists(),
        "blocks": sum(len(d["blocks"]) for d in days),
        "days": days,
        # The canvas is the whole day, every week, so the grid never changes
        # height under you and 03:00 is somewhere you can scroll to rather
        # than somewhere that does not exist.
        "lo": 0, "hi": 24 * 60,
        # …but it opens where the week actually starts, an hour of air above
        # the earliest block, because nobody wants to land on midnight.
        "focus": max(0, (earliest if earliest is not None else 8 * 60) - 60),
        "span": [earliest if earliest is not None else 8 * 60,
                 latest if latest is not None else 18 * 60],
        "claimed": claimed,
        "load": [{"kind": k, "mins": m,
                  "share": (m / claimed) if claimed else 0.0}
                 for k, m in sorted(load.items(), key=lambda kv: -kv[1])],
        "kinds": list(weekplan.KINDS),
        "now_min": minute_now if start <= today <= start + dt.timedelta(6) else None,
        "now": ({"kind": current.kind,
                 "title": _plain(current.label or current.kind, 60),
                 "time": current.time_label,
                 "until": current.end.strftime("%H:%M") if current.end else ""}
                if current else None),
        "next": nxt,
    }


def _tasks(engine, day: dt.date) -> list[dict]:
    """The card: the six most urgent, same rows the view draws."""
    return [_task_row(t, day)
            for t in engine.q.tasks.priorities(today=day, limit=N_TASKS)]


#: Everything that can be done to a checkbox from this surface, and what each
#: one does. Three of them are the answers the view is *for* — done, dropped,
#: ignored — and the rest are their undos. Kept here as one table because the
#: page draws its buttons from the same vocabulary, and a verb invented on
#: either side alone is a button that 400s; `test_hud` asserts they agree.
TASK_ACTIONS = {
    "done":      "ticks the box in the note",
    "dropped":   "strikes it out in the note — the decision survives",
    "ignored":   "leaves the note alone; V stops bringing it up",
    "unignored": "brings it back into every list",
    "open":      "unticks it in the note",
    "doing":     "marks it started in the note",
}


#: What the view's filter row can ask for. `stale` is the one that is new
#: information rather than a re-cut of the same list — see `_task_view`.
TASK_VIEWS = ("open", "overdue", "stale", "ignored", "done")


def _task_row(t, day: dt.date) -> dict:
    """One task, as the view draws it. Same shape the card uses plus the
    fields the row's own actions need — the key, so ignoring survives the
    line moving, and the age, which is the whole reason this view exists."""
    overdue = t.is_overdue(day)
    age = t.age_days(day)
    return {
        "id": t.id,
        "key": t.norm_key,
        "title": _plain(t.text, 160),
        # The design ranks P0/P1/P2. V ranks by overdue-then-due, which is the
        # same idea with the reason still attached, so the label is derived
        # from that rather than from a priority field the vault does not have.
        "rank": "P0" if overdue else ("P1" if t.due else "P2"),
        "why": ("overdue" if overdue else
                f"due {t.due.isoformat()}" if t.due else "no date"),
        "where": t.section or _stem(t.rel).rsplit("/", 1)[-1],
        "area": t.area or "",
        "rel": t.rel,
        "state": t.state,
        "done": t.state == "done",
        "doing": t.state == "doing",
        # Always false here: a row built from a task is a task the filter let
        # through. The ignored view builds its rows from the silence list
        # instead, because an entry outlives the checkbox it was about.
        "ignored": False,
        "age": age if age and age > 0 else 0,
        "carried": t.carried,
        "rotting": t.is_rotting,
    }


def _task_view(engine, day: dt.date, view: str) -> dict:
    """The TASK view's own read — a list long enough to prune, which is the
    one thing the six-row card cannot be.

    Not folded into `snapshot()`: the card is on screen always and this is on
    screen sometimes, and putting 120 rows on a four-second poll to render six
    of them is how a free surface stops being one. The page fetches this when
    the view is open and after every action, the same way `/api/search` works.
    """
    from . import tasks as tasks_mod
    store = engine.q.tasks
    shape = store.backlog_shape(day)
    silenced = store.ignored()

    if view == "ignored":
        # Served from the silence list itself, not from a filtered task query:
        # an entry whose note has since been deleted is still an entry, and it
        # is the one most worth being able to see and clear.
        rows = [{"id": 0, "key": i.norm, "title": _plain(i.text, 160),
                 "rank": "OFF", "why": (f"ignored {i.age_days()}d ago"
                                        if i.at else "ignored"),
                 "where": _stem(i.rel).rsplit("/", 1)[-1] if i.rel else "—",
                 "area": "", "rel": i.rel, "state": "open", "done": False,
                 "doing": False, "ignored": True, "age": i.age_days(),
                 "carried": i.live, "rotting": False,
                 "reason": i.reason} for i in silenced]
    elif view == "done":
        # A fortnight. Long enough to answer "did I already do that?", short
        # enough that the list is not the vault's entire history of ticks.
        since = day - dt.timedelta(days=14)
        done = [t for t in store.done()
                if (t.done_on or t.note_date or dt.date.min) >= since]
        done.sort(key=lambda t: (t.done_on or t.note_date or dt.date.min),
                  reverse=True)
        rows = [_task_row(t, day) for t in done[:N_TASK_VIEW]]
    else:
        # `collapse` for the same reason `priorities` uses it: a task carried
        # forward into five daily notes is one task, and listing it five times
        # is what makes a backlog look unprunable.
        pool = tasks_mod.collapse(store.open())
        if view == "stale":
            # The point of the whole feature, as its own filter: open, undated
            # and older than two months. `backlog_shape` has always counted
            # these as `rotting` and nothing could ever act on them.
            pool = [t for t in pool if t.is_rotting]
        elif view == "overdue":
            pool = [t for t in pool if t.is_overdue(day)]
        pool.sort(key=lambda t: (-t.urgency(day), t.effective_date or dt.date.max))
        rows = [_task_row(t, day) for t in pool[:N_TASK_VIEW]]

    return {
        "view": view,
        "rows": rows,
        "total": len(rows),
        "counts": {"open": shape["open"], "overdue": shape["overdue"],
                   "stale": shape["rotting"], "ignored": len(silenced),
                   "carried": shape["carried"], "live": shape["live"]},
    }


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


def spend_today(s: Settings, day: dt.date | None = None) -> dict:
    """What the day has actually cost, across every surface.

    Read from `hermes.jsonl`, which every process that runs a paid shell
    appends to — the HUD, the REPL, the Telegram service, the phone — so the
    number survives `/new`, survives a restart, and has a $0.40 conversation
    held over Telegram at lunch still on it at six.

    `list_cost_usd` and not `Response.cost`, which is the mistake this was
    written to avoid: on a subscription `cost` is 0.0 *by design* (see
    `engine.py`'s cost model — the allowance consumed travels in
    `data["list_cost_usd"]` instead), so a gauge summing it would sit at
    $0.00 through a day that used half the plan. On an API key the two are the
    same figure, so this is right in both billing modes.

    Filtered by calendar day rather than `hermes.usage(1)`'s rolling 24 hours,
    because the cap it is drawn against is a *daily* one and a bar that still
    carries yesterday evening at nine this morning is not measuring the thing
    on the label.

    A missing file is a day with no paid runs in it, which is $0.00 and not an
    error. A malformed line is skipped rather than raising: this is a gauge,
    and half a day's spend on screen beats a panel that went blank because
    something truncated a write.
    """
    day = day or dt.date.today()
    stamp = day.isoformat()
    used, runs = 0.0, 0
    try:
        with (s.log_dir / "hermes.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if not str(row.get("at", "")).startswith(stamp):
                        continue
                    used += float(row.get("list_cost_usd") or 0.0)
                    runs += 1
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    return {"used": round(used, 6), "turns": runs}


def _nudges(engine, day: dt.date) -> list[dict]:
    try:
        found = engine.nudges.all(today=day)
    except Exception:
        return []
    found.sort(key=lambda n: -n.urgency)
    return [{"key": n.key,
             "kind": n.kind.replace("_", " ").upper(),
             "text": _plain(n.text, 140), "detail": _plain(n.detail, 120),
             "urgency": n.urgency}
            for n in found[:N_NUDGES]]


def _nudges_off(engine) -> list[dict]:
    """What the user has silenced. On the snapshot rather than behind its own
    fetch because it is four rows on a good day and the panel has to be able
    to say "and 3 hidden" without a second round trip — a silence list you
    have to go looking for is one you forget you made."""
    try:
        off = engine.nudges.silenced()
    except Exception:
        return []
    return [{"key": s.key, "text": _plain(s.text, 120),
             "at": s.at[:10], "reason": _plain(s.reason, 80)}
            for s in off[:N_NUDGES_OFF]]


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


def _jalali(day: dt.date, cache: dict) -> dict:
    """The date beside the clock, in the calendar Parsa's year is counted in.

    Split the way `jalali.py` is split, and for the visible reason: the date
    itself is arithmetic, so it is computed here on every snapshot and is
    always right. Only `holiday` needs the network, so only `holiday` is
    behind the cache — and when the lookup fails it stays `None`, which the
    client draws as a quiet "?" rather than as a working day. A dashboard
    that says "not a holiday" because a request timed out is worse than one
    that admits it doesn't know.
    """
    now = time.time()
    date = jalali_mod.convert(day)
    out = {"iso": date.iso, "short": date.short, "label": date.label,
           "month_key": date.month_key, "weekday": date.weekday,
           "gregorian": day.isoformat(), "friday": date.is_friday,
           "holiday": None, "events": [], "off": date.off}
    cached = cache.get("val")
    if cached and cache.get("day") == day.isoformat() and \
            cache.get("at", 0) + JALALI_TTL > now:
        out.update(cached)
        return out
    try:
        full = jalali_mod.lookup(day, timeout=6.0)
        found = {"holiday": full.holiday, "events": list(full.events),
                 "off": full.off}
    except jalali_mod.CalendarError:
        # Nothing to report and nothing to log at: the date is still on
        # screen, and the flag comes back on its own when the network does.
        return out
    cache["at"], cache["day"], cache["val"] = now, day.isoformat(), found
    out.update(found)
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


def _places(engine) -> dict:
    """The PLACES view: every place note, with what the map needs to draw it.

    Not `QueryEngine.places()`, though it reads the same rows: that one is a
    *question* — it filters, ranks, and drops a place with no coordinates from
    any "near" answer. A map has no question; it is every place there is, and
    one without a `location` is still listed, as unplotted, because a café the
    map silently left out reads as a café that does not exist.

    `visits` is the daily notes that link to the place — the same count each
    place note's own `## Visits` block computes in Dataview, so the HUD and
    Obsidian cannot disagree about it. SQL over `links`, like the rest.
    """
    from . import query as query_mod
    q = engine.q
    home = q.resolve_anchor(PLACE_HOME)
    daily = engine.s.folders.daily + "/"
    rows = []
    for r in q.by_type("place"):
        m = r.meta
        loc = query_mod._parse_latlng(m.get("location"))
        stem = r.rel[:-3] if r.rel.endswith(".md") else r.rel
        days = sorted({
            Path(src).stem for (src,) in engine.conn.execute(
                "SELECT src FROM links WHERE dst=? OR dst=?",
                (stem, stem.rsplit("/", 1)[-1]))
            if src.startswith(daily) and _is_iso_day(Path(src).stem)})
        try:
            rating = float(m.get("rating")) if m.get("rating") not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        row = {
            "rel": r.rel,
            # The note's title, not its `name:` — two branches of one café
            # share a name and only the filename tells them apart. The name
            # rides along for the detail card, where it is often the Persian.
            "title": r.title,
            "name": str(m.get("name") or ""),
            "category": str(m.get("category") or "").strip().lower(),
            "lat": loc[0] if loc else None,
            "lng": loc[1] if loc else None,
            "rating": rating,
            "price": str(m.get("price") or ""),
            "neighborhood": str(m.get("neighborhood") or ""),
            "city": str(m.get("city") or ""),
            "visited": str(m.get("visited") or ""),
            "good_for": str(m.get("good-for") or ""),
            "noise": str(m.get("noise") or ""),
            "wifi": query_mod._tri(m.get("wifi")),
            "outdoor": query_mod._tri(m.get("outdoor-seating")),
            "open_late": query_mod._tri(m.get("open-late")),
            "visits": len(days),
            "last_visit": days[-1] if days else "",
            "home_km": None,
        }
        if loc and home:
            row["home_km"] = round(query_mod.haversine_km(home[0], home[1], *loc), 1)
        rows.append(row)
    rows.sort(key=lambda p: (-(p["rating"] or 0), p["title"].lower()))
    return {
        "places": rows,
        "home": ({"lat": home[0], "lng": home[1], "title": home[2]}
                 if home else None),
        "unplotted": sum(1 for p in rows if p["lat"] is None),
        "basemap": "neshan" if engine.s.neshan_api_key else "",
        # For the card's `obsidian://` link, which names the vault by folder.
        "vault": engine.s.vault.name,
        "tile": {"px": TILE_PX, "pad": TILE_PAD,
                 "zmin": TILE_ZOOMS[0], "zmax": TILE_ZOOMS[1]},
    }


def _is_iso_day(stem: str) -> bool:
    try:
        dt.date.fromisoformat(stem)
    except ValueError:
        return False
    return True


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
    # `index.stats()["tasks_open"]` counts every unticked box in the vault —
    # rituals, MR review checklists, lesson rules, the lot — which is 800-odd
    # here and was what the TASKS card put on screen as "OPEN". That number is
    # the complaint, not the answer to it: this one is commitments only,
    # collapsed across the notes they were carried through, and minus what has
    # been silenced.
    backlog = engine.q.tasks.backlog_shape(day)

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
    today_spend = spend_today(engine.s, day)
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
            # Two numbers, because they answer two questions and one of them
            # used to be quietly answering both. `used` is the day's, across
            # every surface, and is what the bar fills against the cap;
            # `session` is what this conversation has cost since `/new`.
            "budget": {"used": today_spend["used"], "cap": cap,
                       "turns": today_spend["turns"],
                       "session": live["session"]["spent"]},
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
        "jalali": _jalali(day, caches.setdefault("jalali", {})),
        "day_events": _events(engine, day, now),
        "week": _week(engine, day, now),
        "tasks": _tasks(engine, day),
        "task_counts": {"open": backlog["open"], "overdue": backlog["overdue"],
                        "stale": backlog["rotting"],
                        "ignored": backlog["ignored"],
                        "carried": backlog["carried"], "live": backlog["live"]},
        "deadlines": deadlines,
        "mems": _memories(engine),
        "nudges": _nudges(engine, day),
        "nudges_off": _nudges_off(engine),
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


class _Scout:
    """The thread that finds pictures, off the thread that answers.

    Two feeds, one worker, because they are the same job seen from two sides.

    *Pushed*: `Session._look` hands over the note paths `shells.ShellEvent`
    has carried since the stream parser was written and which no surface has
    ever drawn. Those become the screenshots pasted into them, and a place
    note becomes a map of where it is. This is the half that runs *in
    parallel with the answer*: the paths arrive while the model is still
    writing, so the picture is on screen before the sentence about it is.

    **The two feeds of note paths are not the same signal, and the first
    version of this spent them as if they were.** `rel` is what a call named
    in its own argument — `vault_read(rel=X)` means the model chose X.
    `rels` is every `NN - ….md` scraped out of what came back, which for a
    `Glob` over a folder is a *directory listing*. On 2026-09-08 a question
    about three places globbed `09 - …/05 - Locations/`, and this class read
    the twelve alphabetically-first cafés in the vault as twelve notes the
    answer was about: nine maps of places nobody had mentioned, and nine
    Neshan requests to draw them.

    So a listed path buys less than a chosen one. A chosen note — or one the
    answer *cited*, which is V's own statement of what it used — can become a
    map. A listed one gets images only: those are a local file read, they
    cost nothing, and a search that happens to surface a bug report with a
    screenshot in it is exactly the ambient case this was built for. Three of
    them per turn, because past that a result set has stopped being an answer
    and gone back to being a list.

    *Polled*: `media.Spool`, which is how a chart rendered inside an MCP
    subprocess — a different process, with no handle on this one — reaches
    the viewport.

    Deliberately a thread and not the engine thread. A `place_map` is a
    network round trip to Neshan; on `_progress`'s thread that would be
    latency added to the answer in order to decorate it. Nothing here can
    fail loudly either: a picture that could not be found is a frame that
    does not appear, never a fault in the header for something nobody asked
    for out loud.
    """

    def __init__(self, s: Settings, session: Session):
        self.s = s
        self.session = session
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.spool = media_mod.Spool(s)
        self.cache: dict = {}          # the attachment index, TTL'd inside
        self.seen: set[str] = set()    # notes already scouted this turn
        self.loose = 0                 # of those, how many came from a list
        self.maps = 0                  # and how many Neshan calls it cost
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="hud-scout")
        self.thread.start()

    def saw(self, chosen, listed) -> None:
        """Called from the turn's thread. Never blocks it: a full queue means
        the scout is behind, and a dropped picture is not worth a millisecond
        of the answer. `(None, None)` is the new-turn sentinel."""
        try:
            self.q.put_nowait((chosen, listed))
        except queue.Full:
            pass

    def _run(self) -> None:
        while not self.stop_flag.is_set():
            chosen: list = []
            listed: list = []
            try:
                chosen, listed = self.q.get(timeout=SPOOL_POLL)
            except queue.Empty:
                pass
            else:
                if chosen is None:     # a new turn: forget the last one's notes
                    self.seen.clear()
                    self.loose = self.maps = 0
                    chosen, listed = [], []
            try:
                found = self._pictures(chosen or [], listed or [])
                if found:
                    self.session.show(found)
                items = self.spool.take()
                if items:
                    self.session.show(items)
            except Exception:          # noqa: BLE001 — see the class docstring
                pass

    def _pictures(self, chosen, listed) -> list:
        """`chosen` may become a map; `listed` may only ever become images.

        The asymmetry is the whole point — see the class docstring for the
        Monday it was written on.
        """
        out = []
        for rel in chosen:
            out.extend(self._one(rel, may_map=True))
        for rel in listed:
            if self.loose >= N_LOOSE:
                break
            if rel in self.seen:
                continue
            self.loose += 1
            out.extend(self._one(rel, may_map=False))
        return out

    def _one(self, rel: str, *, may_map: bool) -> list:
        if rel in self.seen or len(self.seen) >= N_SCOUT:
            return []
        self.seen.add(rel)
        if media_mod.is_place(self.s, rel):
            if not may_map or self.maps >= N_MAPS:
                return []
            self.maps += 1
            found = media_mod.place_map(self.s, rel)
            return [found] if found is not None else []
        return media_mod.images_in_note(self.s, rel, cache=self.cache)

    def stop(self) -> None:
        self.stop_flag.set()


class _Tiles:
    """The PLACES basemap: Neshan statics on a fixed grid, cached for good.

    Cell `(x, y)` at zoom `z` is the `TILE_PX` square of world pixels starting
    at `(x·TILE_PX, y·TILE_PX)` — the same Web Mercator `mercator_px` uses, so
    a pin projected by the page lands on the street it is on. The image is
    fetched `TILE_PAD` larger on every side and the page crops it; see
    `TILE_PAD` for why.

    Runs on the HTTP handler's thread, never the engine's: a tile is a network
    round trip, and an answer must not wait behind someone dragging a map. No
    engine is needed — the cache is a directory and the fetch is `mapsearch`.

    What it sends Neshan is a grid cell, not a place: the centre of whichever
    square is on screen. `place_map` already sends the exact coordinates of a
    café to the same service, so this opens no new class of leak — it is the
    same one, coarser.
    """

    def __init__(self, s: Settings):
        self.s = s
        self.dir = s.state_dir / "renders" / "tiles"
        self._lock = threading.Lock()
        self._spent: list[float] = []      # when each fresh fetch happened
        self._dead: dict[str, tuple[float, str]] = {}   # cell → when, why
        self._busy: dict[str, threading.Event] = {}

    def path(self, z: int, x: int, y: int) -> Path:
        lo, hi = TILE_ZOOMS
        if not lo <= z <= hi:
            raise HudError(400, f"zoom {z} is outside {lo}–{hi}")
        cells = _world_px(z) // TILE_PX
        if not (0 <= x < cells and 0 <= y < cells):
            raise HudError(400, "no such tile")
        return self.dir / str(z) / f"{x}-{y}.png"

    def get(self, z: int, x: int, y: int) -> bytes:
        dest = self.path(z, x, y)
        key = f"{z}/{x}/{y}"
        if dest.is_file():
            return dest.read_bytes()
        if not self.s.neshan_api_key:
            raise HudError(404, "no basemap — neshan_api_key is not set")
        # One fetch per cell, however many requests ask for it at once: the
        # page asks for every visible tile together, and a pan can ask twice.
        with self._lock:
            now = time.time()
            when, why = self._dead.get(key, (0.0, ""))
            if now - when < TILE_RETRY:
                raise HudError(503, f"failed {now - when:.0f}s ago: {why}")
            # Again, under the lock: another request may have landed it
            # between the check above and here.
            if dest.is_file():
                return dest.read_bytes()
            wait = self._busy.get(key)
            if wait is None:
                self._spent = [t for t in self._spent if now - t < 3600.0]
                if len(self._spent) >= TILE_BUDGET:
                    raise HudError(429, f"tile budget spent — {TILE_BUDGET} "
                                        "new tiles an hour; cached ones still draw")
                self._spent.append(now)
                self._busy[key] = threading.Event()
        if wait is not None:
            wait.wait(30)
            if dest.is_file():
                return dest.read_bytes()
            raise HudError(503, "tile fetch failed")
        try:
            blob = self._fetch(z, x, y)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(blob)
            tmp.replace(dest)
            return blob
        except HudError as exc:
            with self._lock:
                self._dead[key] = (time.time(), exc.message)
                # A request that failed is refunded: a dead network must not
                # spend the hour's budget on tiles it never got.
                if self._spent:
                    self._spent.pop()
            raise
        except OSError as exc:
            raise HudError(503, f"tile cache unwritable: {exc}") from exc
        finally:
            with self._lock:
                done = self._busy.pop(key, None)
            if done is not None:
                done.set()

    def _fetch(self, z: int, x: int, y: int) -> bytes:
        from . import mapsearch            # httpx, and only on this path
        lat, lng = mapsearch.mercator_latlng(
            (x + 0.5) * TILE_PX, (y + 0.5) * TILE_PX, z)
        side = TILE_PX + 2 * TILE_PAD
        # Once more on a failure, after a breath. Seen on the first real run:
        # one cell of six came back bad and the same request a minute later
        # was fine — and a hole in the city is worse than half a second.
        for attempt in (1, 2):
            try:
                blob = mapsearch.static_map(lat, lng, z, side, side,
                                            settings=self.s)
                break
            except mapsearch.MapSearchError as exc:
                if attempt == 2:
                    raise HudError(503, str(exc)) from exc
                time.sleep(0.5)
        if not blob:
            raise HudError(404, "no basemap — neshan_api_key is not set")
        return blob

    def cached(self) -> int:
        try:
            return sum(1 for _ in self.dir.glob("*/*.png"))
        except OSError:
            return 0


def _world_px(z: int) -> int:
    """The world's width in pixels at `z`. `mapsearch.TILE_PX` is 256 and the
    grid here is 512 — two different tiles, and easy to confuse."""
    return 256 * (2 ** z)


class Hud:
    """The application. Owns the engine thread, the session and the caches."""

    def __init__(self, s: Settings | None = None):
        self.s = s or default_settings
        self.session = Session()
        self.scout = _Scout(self.s, self.session)
        self.session.watch = self.scout.saw
        self.bridge = _Bridge(self.s, self.session)
        self.tiles = _Tiles(self.s)
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

    def tasks(self, view: str = "open") -> dict:
        """The TASK surface. Free — SQL over the mirror, like every other read
        on this page."""
        if view not in TASK_VIEWS:
            raise HudError(400, f"unknown view {view!r}")
        return self.bridge.call(
            lambda e: _task_view(e, dt.date.today(), view))

    def places(self) -> dict:
        """The PLACES surface. Free, like the rest: rows and links out of the
        mirror. The only thing on that view that reaches a network is the
        basemap, and that is `tile()`, which does not go through here."""
        out = self.bridge.call(_places)
        tiles = getattr(self, "tiles", None)
        out["tiles_cached"] = tiles.cached() if tiles is not None else 0
        return out

    def tile(self, z: int, x: int, y: int) -> bytes:
        """One basemap cell, as PNG bytes. Not on the engine thread — see
        `_Tiles`. A `BareHud` in the tests has none, and says so."""
        tiles = getattr(self, "tiles", None)
        if tiles is None:
            raise HudError(503, "no basemap on this server")
        return tiles.get(z, x, y)

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
        # The citations, as well as the tool stream. A structured answer —
        # "where is Kaboom?", answered from SQL — runs no shell and so emits
        # no `ShellEvent` at all, and the citation is the only trace it leaves
        # of which note it read. Without this the viewport would be blind to
        # exactly the cheap, instant questions it is best at illustrating.
        if self.session.watch is not None:
            cites = [media_mod.rel_from_citation(c) for c in turn.cites]
            # Chosen, not listed: a citation is V's own statement of what it
            # used, which is the strongest signal on the page that a note is
            # what the answer was actually about.
            self.session.watch([c for c in cites if c], [])
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
            # The same three verbs the REPL and the `⊗` button have. Typed
            # here mainly to restore something: silencing has a button on the
            # panel, and the panel is right there.
            verb, _, key = rest.partition(" ")
            verb, key = verb.strip(), key.strip()
            if verb == "off" and key:
                with self._snap_lock:
                    self._snap = None       # the panel just lost a row
                return self._land(self.bridge.call(
                    lambda e: e.nudge_silence(key), timeout=60))
            if verb == "off":
                return self._land(self.bridge.call(
                    lambda e: e.nudges_silenced(), timeout=60))
            if verb == "on" and key:
                with self._snap_lock:
                    self._snap = None       # …and just gained one back
                return self._land(self.bridge.call(
                    lambda e: e.nudge_restore(key), timeout=60))
            if verb:
                return self._say("`/nudges` · `/nudges off [key]` · "
                                 "`/nudges on <key>`", state="refused",
                                 route="—")
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
            day = spend_today(self.s)
            return self._say(
                f"**${day['used']:.4f} today** of a ${cap:.2f} cap, over "
                f"{day['turns']} turn{'s' if day['turns'] != 1 else ''} on "
                f"every surface — this window, the terminal and Telegram all "
                f"spend from it.\n\n"
                f"**${live['spent']:.4f} this conversation** over "
                f"{live['turns']} turn{'s' if live['turns'] != 1 else ''}, "
                f"{live['local']} of them free. `/new` resets that half and "
                f"leaves the day's alone.\n\n"
                f"The gauge on the left is the same pair of numbers, live.")

        if f"/{cmd}" in known:
            # In the table, handled in the page. Reaching here means the
            # client sent something it should have kept.
            raise HudError(400, f"/{cmd} is handled in the page")
        return self._say(f"Unknown command `/{cmd}` — press `/` for the list.",
                         state="refused", route="—")

    def task(self, state: str, *, task_id: int = 0, key: str = "") -> dict:
        """The design's checkbox, wired to the real one — and to the two other
        things that can happen to a task.

        `done` · `dropped` · `open` · `doing` go through `tasks.complete`,
        which rewrites the checkbox in the note, commits it per-path as
        `[bot]` and is revertible with `v undo` — rather than flipping a row
        in the index, because the Markdown is the source of truth and an index
        the vault disagrees with is the bug this whole package is built to
        avoid.

        `ignored` · `unignored` write to `task_ignores` and touch **no note
        at all**. That separation is the feature: a task can be got rid of
        without editing a page the user wrote by hand, which is what made
        pruning the backlog something he would actually do. The two paths are
        deliberately not merged into one "state" column for the same reason —
        one of them is a fact about the vault and the other is an instruction
        to V, and conflating them is how "I don't want to hear about this"
        turns into a silent edit of a daily note from July.

        `key` addresses a task the way the silence list does — by its
        normalised text — so un-ignoring works for an entry whose checkbox has
        since moved, been rewritten, or had its note deleted. Every other verb
        needs a line to write to, so it needs an id.
        """
        def work(engine):
            from . import tasks as tasks_mod
            store = engine.q.tasks
            if state not in TASK_ACTIONS:
                raise HudError(400, f"unknown state {state!r}")

            if state == "unignored":
                target = key or (store.get(task_id).norm_key
                                 if task_id and store.get(task_id) else "")
                if not target:
                    raise HudError(400, "unignore needs a key or a live task")
                return {"ok": True, "unchanged": not store.unignore(target),
                        "state": state}

            if state == "ignored":
                if key and not task_id:
                    # An entry re-silenced from the ignored list — no live
                    # checkbox behind it, so there is only the phrase.
                    store.ignore(key, reason="hud")
                    return {"ok": True, "unchanged": False, "state": state}
                t = store.get(task_id)
                if t is None:
                    raise HudError(404, f"no task {task_id}")
                if store.is_ignored(t):
                    return {"ok": True, "unchanged": True, "state": state}
                store.ignore(t, reason="hud")
                return {"ok": True, "unchanged": False, "state": state,
                        "key": t.norm_key}

            valid = {"done": tasks_mod.DONE, "open": tasks_mod.OPEN,
                     "doing": tasks_mod.DOING, "dropped": tasks_mod.DROPPED}
            t = store.get(task_id)
            if t is None:
                raise HudError(404, f"no task {task_id}")
            if t.state == valid[state]:
                return {"ok": True, "unchanged": True, "state": state}
            store.complete(t, engine.vault, state=valid[state],
                           today=dt.date.today())
            return {"ok": True, "unchanged": False, "state": state}
        out = self.bridge.call(work, timeout=60)
        self.caches.pop("drift", None)     # the note just moved under the index
        return out

    def block(self, action: str, body: dict) -> dict:
        """Add, move or drop one time block, from the WEEK view.

        The same three verbs the CLI and the MCP tools have, reached from a
        click instead of a sentence. Every one of them is a `[bot]` commit on
        one note and reverts with `v undo`, exactly like a tick on a checkbox
        from the TASKS view — this surface has never been read-only, and a
        calendar you can only look at is a screenshot.

        `line` is the block's own written text, which is how the browser names
        it. That is deliberate: `vault.edit` refuses a line it cannot find
        exactly once, so a stale tab trying to move a block somebody already
        moved fails loudly instead of clobbering its neighbour.
        """
        def work(engine):
            from . import weekplan as wp_mod
            wp = engine.q.weekplan

            def as_date(value, field):
                got = notes_mod.as_date(value) if value else None
                if got is None:
                    raise HudError(400, f"{field} must be YYYY-MM-DD")
                return got

            def as_time(value, field):
                got = wp_mod._parse_time(str(value or ""))
                if got is None:
                    raise HudError(400, f"{field} must be HH:MM")
                return got

            if action == "add":
                day = as_date(body.get("date"), "date")
                kind = str(body.get("kind") or "").lower()
                if kind not in wp_mod.KINDS:
                    raise HudError(400, "kind must be one of: "
                                        + ", ".join(wp_mod.KINDS))
                start = as_time(body.get("from"), "from")
                end = as_time(body.get("to"), "to")
                if wp_mod._minutes(end) <= wp_mod._minutes(start):
                    raise HudError(400, "a block has to end after it starts")
                label, where = wp_mod._split_location(
                    str(body.get("label") or "").strip())
                said = wp.add(wp_mod.Block(day, start, end, kind, label,
                                           where or str(body.get("where") or "")))
                wp.sync_daily_note(day)
                return {"ok": True, "said": said}

            line = str(body.get("line") or "")
            if not line.strip():
                raise HudError(400, "line is required — it names the block")
            day = as_date(body.get("date"), "date")

            if action == "drop":
                try:
                    said = wp.remove(day, line)
                except ValueError as exc:
                    raise HudError(409, str(exc))
                wp.sync_daily_note(day)
                return {"ok": True, "said": said}

            if action == "move":
                changes = {}
                if body.get("to_date"):
                    changes["date"] = as_date(body["to_date"], "to_date")
                if body.get("from"):
                    changes["start"] = as_time(body["from"], "from")
                if body.get("to"):
                    changes["end"] = as_time(body["to"], "to")
                if body.get("kind"):
                    changes["kind"] = str(body["kind"]).lower()
                if body.get("label") is not None:
                    # The form shows `label @where` as one field, so what it
                    # sends back is the whole truth about both — including an
                    # `@location` that has been deleted. Setting location only
                    # when one was typed made it sticky and invisible: rename
                    # "draft intro @home" to "reread Turpin" and the block
                    # kept a location the form had never shown you.
                    label, where = wp_mod._split_location(str(body["label"]).strip())
                    changes["label"] = label
                    changes["location"] = where
                if not changes:
                    raise HudError(400, "nothing to change")
                try:
                    said = wp.move(day, line, **changes)
                except ValueError as exc:
                    raise HudError(409, str(exc))
                wp.sync_daily_note(day)
                moved = changes.get("date")
                if moved and moved != day:
                    wp.sync_daily_note(moved)
                return {"ok": True, "said": said}

            raise HudError(400, "action must be add, move or drop")

        out = self.bridge.call(work, timeout=60)
        with self._snap_lock:
            self._snap = None              # the week just changed under the panel
        self.caches.pop("drift", None)
        return out

    def nudge(self, action: str, key: str) -> dict:
        """`⊗` on a nudge, and `↺` on the silence list under it.

        The shape is `task`'s `ignored`/`unignored` pair and the reasoning is
        the same one, arrived at from the other end: a task is silenced
        without editing the note it lives on, and a nudge has no note to edit
        in the first place — it is a *reading* of the vault, recomputed every
        four seconds. So this endpoint cannot damage anything, and the only
        thing it can get wrong is hiding something the user wanted, which the
        list right under the panel exists to make visible and one click to
        undo.

        Silences are permanent until restored rather than timed. A snooze was
        the obvious alternative and was not chosen: "remind me in a week"
        against a check that fires on a *threshold* — five days without a
        workout, three days in the inbox — is a promise to raise it again in a
        week whether or not anything changed, which is the behaviour being
        complained about with an extra step in front of it.
        """
        if action not in ("silence", "restore"):
            raise HudError(400, "action must be 'silence' or 'restore'")
        key = key.strip()
        if not key:
            raise HudError(400, "key is required")

        def work(engine):
            if action == "restore":
                return {"ok": True, "action": action, "key": key,
                        "unchanged": not engine.nudges.restore(key)}
            try:
                engine.nudges.silence(key, reason="hud")
            except ValueError as exc:
                raise HudError(400, str(exc))
            return {"ok": True, "action": action, "key": key,
                    "unchanged": False}
        out = self.bridge.call(work, timeout=60)
        # The panel is drawn from the cached snapshot, so without this the
        # nudge stays on screen until the next poll and the click reads as
        # having done nothing.
        with self._snap_lock:
            self._snap = None
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
        if self.scout is not None:
            self.scout.stop()
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

#: The HUD's own policy. Everything it needs is on this origin.
PAGE_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")

#: The policy for a rendered artifact shown *inside* the viewport, and the
#: only place this server serves a document it did not write itself.
#:
#: Three things it must allow that the HUD's own policy does not, each of
#: them a fact about `render.py` rather than a loosening on principle:
#: inline `<style>` and `<script>`, because a self-contained chart is
#: inline by construction; `data:` images, for the same reason; and
#: `*.neshan.org`, because `place_html` is that module's one documented CDN
#: exception and a map with no tile server under it is a grey box.
#:
#: This is still strictly tighter than what these files get today, which is
#: `webbrowser.open()` — a real tab, with no policy at all. And the frame
#: carries `sandbox="allow-scripts"`, so the document runs in an opaque
#: origin: it can draw itself and reach Neshan, and it can read nothing of
#: the HUD's — not the DOM around it, not its storage, not its cookies.
ARTIFACT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline' https://static.neshan.org; "
    "script-src 'unsafe-inline' https://static.neshan.org; "
    "img-src data: blob: https://*.neshan.org; "
    "font-src data: https://static.neshan.org; "
    "connect-src https://*.neshan.org; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


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
              extra: dict | None = None, *, csp: str = "",
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page talks to nothing but itself. A CSP that says so is one
        # line and it is what makes "a browser tab" an acceptable place to
        # render a vault: no font CDN, no analytics, no exfiltration path for
        # a note that ends up in the DOM.
        #
        # `csp` overrides it for exactly one response — a rendered chart or
        # map framed in the viewport, which is a *document* and needs its own
        # rules. See `ARTIFACT_CSP`.
        self.send_header("Content-Security-Policy", csp or PAGE_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
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
            if path == "/api/tasks":
                from urllib.parse import parse_qs
                view = (parse_qs(query).get("view") or ["open"])[0]
                return self._json(self.app.tasks(view))
            if path == "/api/places":
                return self._json(self.app.places())
            if path == "/api/tile":
                return self._tile(query)
            if path == "/api/search":
                from urllib.parse import parse_qs, unquote
                q = (parse_qs(query).get("q") or [""])[0]
                return self._json(self.app.search(unquote(q)))
            if path == "/api/media":
                from urllib.parse import parse_qs
                return self._media((parse_qs(query).get("id") or [""])[0])
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
                # No default. This is a write endpoint, and `or "done"` meant
                # a body that failed to parse — a bad request, a truncated
                # POST — was read as "tick a task", which is the one verb here
                # that should never be reached by accident. Watched it happen
                # seventeen times from a malformed client; it only did no harm
                # because the id defaulted to 0 as well.
                state = body.get("state")
                if not isinstance(state, str) or not state:
                    raise HudError(400, "state is required: "
                                        + ", ".join(TASK_ACTIONS))
                return self._json(self.app.task(
                    state,
                    task_id=int(body.get("id") or 0),
                    key=str(body.get("key") or "")))
            if path == "/api/command":
                text = (body.get("text") or "").strip()
                if not text.startswith("/"):
                    raise HudError(400, "not a command")
                return self._json(self.app.command(text))
            if path == "/api/frames":
                # One verb, and it is the only one there can be: the viewport
                # is a *view*, so a frame is not something to delete — the
                # file is still where it was and the note still embeds it.
                # This says "stop showing me these", nothing more.
                if body.get("action") != "clear":
                    raise HudError(400, "action must be 'clear'")
                self.app.session.clear_frames()
                return self._json({"ok": True,
                                   "live": self.app.session.live()})
            if path == "/api/nudge":
                # No default on the action, same rule as `/api/task`: a
                # truncated POST must not be readable as "hide this".
                action = body.get("action")
                if not isinstance(action, str) or not action:
                    raise HudError(400, "action is required: silence, restore")
                return self._json(self.app.nudge(
                    action, str(body.get("key") or "")))
            if path == "/api/block":
                action = body.get("action")
                if not isinstance(action, str) or not action:
                    raise HudError(400, "action is required: add, move, drop")
                return self._json(self.app.block(action, body))
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

    def _tile(self, query: str) -> None:
        """A basemap cell. Addressed by grid coordinates, never by a path, so
        like `/api/media` there is nothing here to traverse with — three
        integers, range-checked by `_Tiles.path` before the disk is touched.

        Cached by the browser for a week: a cell is immutable once fetched,
        and a pan back over ground already seen should cost nothing at all.
        """
        from urllib.parse import parse_qs
        args = parse_qs(query)
        try:
            z, x, y = (int((args.get(k) or [""])[0]) for k in ("z", "x", "y"))
        except ValueError:
            raise HudError(400, "z, x and y must be integers") from None
        body = self.app.tile(z, x, y)
        self._send(200, body, "image/png", cache="private, max-age=604800")

    def _media(self, fid: str) -> None:
        """One frame's bytes, addressed by handle rather than by path.

        There is no filename anywhere in this request. `id` is a hash, and it
        is looked up in a registry the session filled when it decided to show
        the picture — so this endpoint cannot be pointed at a file the HUD did
        not already put on screen, and path traversal is not defended against
        so much as unsayable.

        A rendered chart or map is a document and is framed rather than drawn,
        so it gets `ARTIFACT_CSP` instead of the HUD's own; everything else is
        an image and gets the ordinary one. `max-age` is short and private:
        these are immutable files with timestamped names, and re-fetching one
        every time the filmstrip is clicked back and forth is the one thing on
        this page that would touch the disk for no reason.
        """
        path = self.app.session.media_path(fid) if fid else None
        if path is None or not path.is_file():
            return self._json({"error": "no such frame"}, status=404)
        kind = media_mod.kind_for(path)
        try:
            body = path.read_bytes()
        except OSError as exc:
            return self._json({"error": f"unreadable: {exc}"}, status=404)
        self._send(200, body, media_mod.mime_for(path),
                   csp=ARTIFACT_CSP if kind == "page" else "",
                   cache="private, max-age=300")

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
