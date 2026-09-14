"""httpapi — the phone's transport, and the first one that isn't someone else's.

`telegram.py` proved the seam: an update comes in, `engine.handle()` runs, a
`Response` goes back out, and nothing in `core/` had to learn what Telegram
was. This is the same shim against a different wire — and one different
premise. Telegram works, but every word of it, every voice note and every
photograph of a book page, travels through a company's servers and sits in
their storage. For a surface whose whole content is a personal journal, a
finance ledger and a CRM, that is the wrong shape. Here, nothing leaves the
machine but the answer, and it goes to exactly one phone.

```
phone ──TLS(mutual)──►  _Handler  ──►  _Bridge  ──►  Engine.handle  ──►  vault
  ▲                        ▲                              │
  │                        │                              ▼
  └── SSE /v1/events ◄── Broadcast ◄──── Scheduler (reminders, brief, nudges)
```

**Access control is the handshake, not a check behind it.** `pki.py` issues one
certificate per device from a private CA; this server sets `CERT_REQUIRED`
against that CA alone, so an unenrolled client never completes TLS and never
reaches a request handler at all. There is deliberately no bearer token on top:
the token would have to ship inside the same APK as the client key, so it would
be guarded by exactly the thing it was meant to back up, and a second gate that
falls whenever the first one does is decoration. What *is* worth adding is
narrowing where the socket listens — see `resolve_bind()`.

The one thing the handshake cannot do is *un*-authorise: with no CRL, a revoked
certificate still chains to the CA and still completes TLS, so cutting off a
lost phone happens in the handler instead — against the enrolled set read live
off disk, never a copy memorised at boot. See `Enrolment`.

**Two things are refused rather than made to work.** There is no unauthenticated
health endpoint: an endpoint that answers before the handshake is an endpoint
that tells a scanner this is here, and the design's own note about not replying
to strangers applies to ports as much as to chats. And there is no HTTP
fallback — no redirect from 80, no `--insecure` flag — because a flag that
turns off the only access control in the system is a flag that eventually gets
left on.

Everything else mirrors `telegram.py` on purpose: one `Engine` owned by one
thread (a sqlite3 connection belongs to whichever thread opened it), one shared
`Transcriber` and one shared synthesizer behind locks, the scheduler on
its own thread with its own engine, and replies carrying both text and speech.
"""

from __future__ import annotations

import datetime as dt
import json
import queue
import re
import ssl
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from . import pki
from .files import (ATTACH_LIMIT, file_meta, mime_for,
                    save_attachment, take_queued_files)
from .config import Settings, settings as default_settings
from . import persona as persona_mod
from . import tts as tts_mod
from .engine import Engine

API_VERSION = 1

# How long a request may occupy a worker before the client is told to stop
# waiting. Generous because an escalation to the model genuinely takes ~20s and
# the phone showing a spinner is the correct behaviour for that; not unbounded,
# because a wedged shell would otherwise hold the connection until the app is
# force-stopped.
CALL_TIMEOUT = 300.0

# A voice reply is capped for the same reason `telegram._for_speech` caps one:
# the full answer is already on screen, and a four-minute synthesis of it is a
# thing nobody asked for and everybody waits through.
SPEAK_LIMIT = 2000


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _scrub(exc: object) -> str:
    """Never let a certificate path or a token fragment into a log line."""
    return re.sub(r"(?i)(token|secret|key|password)[=:\s]+\S+", r"\1=***",
                  str(exc))[:400]


# ---- who is allowed in ----------------------------------------------------
class Enrolment:
    """The set of enrolled device CNs, re-read from disk as it changes.

    Reading it once at start-up was a bug with a sharp edge. There is no CRL —
    `pki.revoke()` deletes a certificate and the directory listing *is* the
    revocation list — so the 403 in `_dispatch` is the entire revocation
    mechanism, and against a set memorised at boot it was checking a snapshot.
    `cli phone revoke pixel` reported success, deleted the certificate, and the
    lost phone went on being served until somebody restarted the service. The
    one moment revocation has to work is the one moment you are least likely to
    be sitting at the machine.

    Re-reading on every request is the right semantics and the wrong cost:
    `pki.enrolled()` spawns openssl once per certificate to read its subject.
    So the check is two-stage — stat the certificate directory, and only pay
    for openssl when its mtime has moved. Enrolling writes new files and
    revoking unlinks them; both bump the directory's mtime, and those are the
    only two events that can change the answer.

    A directory that cannot be stat'd is treated as empty rather than as an
    error. The certificates being gone *is* everything being revoked, and
    failing open here would be worse than the outage.
    """

    def __init__(self, s: Settings):
        self.s = s
        self._lock = threading.Lock()
        self._names: frozenset[str] = frozenset()
        self._stamp: tuple[int, int] | None = None
        self.reload()

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = pki.pki_dir(self.s).stat()
        except OSError:
            return None
        # The inode too, because the whole directory being replaced (a restore,
        # a symlink repointed) can leave the mtime looking untouched.
        return (st.st_mtime_ns, st.st_ino)

    def reload(self) -> frozenset[str]:
        """Re-read unconditionally, whatever the directory looks like."""
        stamp = self._stat()
        names = (frozenset(pki.enrolled(self.s)) if stamp is not None
                 else frozenset())
        with self._lock:
            self._names, self._stamp = names, stamp
        return names

    @property
    def names(self) -> frozenset[str]:
        stamp = self._stat()
        with self._lock:
            if stamp == self._stamp:
                return self._names
        # Deliberately outside the lock: `reload()` shells out to openssl, and
        # holding a mutex across a subprocess would queue every request in
        # flight behind the slowest one.
        return self.reload()

    def allows(self, cn: str) -> bool:
        return cn in self.names


# ---- proactive: one bus, many listeners ----------------------------------
class Broadcast:
    """Reminders, the brief and nudges, fanned out to whoever is connected.

    The backlog is the point. A phone is asleep, out of signal, or simply not
    running the app for most of the day, and a reminder delivered into a socket
    that nobody was holding is a reminder that silently never fired — the exact
    failure `Scheduler`'s catch-up-on-boot exists to prevent, reintroduced one
    layer higher. So every event gets a sequence number, the last `keep` are
    held, and a client reconnects with `?since=` and is handed everything it
    missed before the stream goes live. Bounded, because this is a delivery
    buffer and not a log: what is durable is already a note.
    """

    def __init__(self, keep: int = 100):
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._backlog: deque[dict] = deque(maxlen=keep)
        self._seq = 0

    def publish(self, kind: str, text: str, data: dict | None = None) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "kind": kind,
                "text": text,
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                "data": data or {},
            }
            self._backlog.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:      # a stalled client must not stall the rest
                pass
        return event

    def subscribe(self, since: int = 0) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            for event in self._backlog:
                if event["seq"] > since:
                    q.put_nowait(event)
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subs)


def build_sink(bus: Broadcast, s: Settings | None = None):
    """A `Scheduler(..., sink=...)` that publishes instead of printing.

    Same contract `telegram.build_sink` implements against the same jobs — the
    scheduler hands over something with a `.render()` (a `Response`) or a plain
    string, and no job has to know which surface is listening.
    """
    s = s or default_settings

    def sink(source: str, item) -> bool:
        """Returns False when a reminder had no one to go to.

        The backlog is a reconnect buffer, not a mailbox: it is in memory, it
        holds the last `keep` events, and it dies with the process. For the
        brief or a brag draft that is exactly right. For a reminder it is not
        — `Scheduler._deliver` would settle the row, and a phone that was
        simply not running that morning would have nothing, forever.

        So a reminder with no live listener is not published at all and is
        reported undelivered; it stays due, and the next one-minute tick
        offers it again. The retry *is* the backlog, and it is a durable one,
        because it lives in SQLite. Everything else publishes as before.
        """
        name, payload = item if isinstance(item, tuple) else (source, item)
        items = payload if isinstance(payload, list) else [payload]
        live = bus.listeners > 0
        ok = True
        for p in items:
            if p is None:
                continue
            text = getattr(p, "render", lambda: str(p))()
            if not text or not text.strip():
                continue
            if getattr(p, "route", "") == "reminder:fire" and not live:
                ok = False
                continue
            data = getattr(p, "data", None) or {}
            bus.publish(str(name), text, {
                "buttons": _buttons(getattr(p, "buttons", None)),
                "route": getattr(p, "route", ""),
                # A scheduled job can hand over a file the same way a turn
                # can — the weekly rollup's chart, a brag draft — and the
                # phone renders it with the same chip either way.
                "files": [file_meta(s, rel) for rel in (data.get("files") or [])
                          if isinstance(rel, str)],
            })
        return ok
    return sink


# ---- the engine, on the one thread allowed to touch it -------------------
class _Bridge:
    """Serialises every engine call onto a single thread, and blocks the
    caller until it returns.

    An HTTP server is a thread pool by construction, and `Engine` is not
    thread-safe by construction — it holds a sqlite3 connection, which may only
    be used from the thread that opened it. `mcp/server.py` solves the same
    problem by giving every worker thread its own engine; that is wrong here,
    because an engine is also a *conversation* (`start_session`, the Hermes
    thread, `_last_capture`), and one conversation split across four sockets is
    four strangers again — the failure sessions were built to fix.

    So: one engine, one thread, a queue in front of it. Requests are answered in
    the order they arrive, which for one user with one phone is also the order
    they were asked in.
    """

    def __init__(self, s: Settings):
        self.s = s
        self._in: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._boot_error: BaseException | None = None
        self._stt = None
        self._stt_lock = threading.Lock()
        self._tts = None
        self._tts_lock = threading.Lock()
        # Utterances being transcribed as they are spoken, and answers being
        # synthesized as they are read. Both are keyed by an id the phone
        # coins, both are swept by age, and both are deliberately in front of
        # the engine thread rather than on it: whisper and Piper hold their own
        # locks and must not queue behind a model turn that is still thinking.
        self._streams: dict[str, tuple[float, "object"]] = {}
        self._streams_lock = threading.Lock()
        self._spools: dict[str, SpeechSpool] = {}
        self._spools_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="v-http-engine")
        self._thread.start()

    # ---- the owning thread -------------------------------------------
    def _run(self) -> None:
        import atexit
        try:
            engine = Engine(self.s, surface=persona_mod.PHONE)
            engine.start_session()
        except BaseException as exc:        # noqa: BLE001 — reported to callers
            self._boot_error = exc
            self._ready.set()
            return
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
                except BaseException as exc:    # noqa: BLE001 — travels to caller
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

    # ---- callers ------------------------------------------------------
    def call(self, fn, timeout: float = CALL_TIMEOUT):
        self._ready.wait(120)
        if self._boot_error is not None:
            raise ApiError(503, f"engine failed to start: "
                                f"{_scrub(self._boot_error)}")
        box: dict = {}
        done = threading.Event()
        self._in.put((fn, box, done))
        if not done.wait(timeout):
            raise ApiError(504, "V is still working on the previous message")
        if "err" in box:
            err = box["err"]
            if isinstance(err, ApiError):
                raise err
            raise ApiError(500, f"{type(err).__name__}: {_scrub(err)}")
        return box["val"]

    def stop(self) -> None:
        self._in.put((None, {}, threading.Event()))

    # ---- speech, shared and serialised (see telegram.py for why) ------
    def transcriber(self):
        with self._stt_lock:
            if self._stt is None:
                from . import stt
                t = stt.Transcriber(self.s)
                t.load()
                self._stt = t
            return self._stt

    def transcribe(self, data: bytes, suffix: str = ".ogg") -> str:
        from . import stt
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / f"voice{suffix}"
            src.write_bytes(data)
            # An explicit destination, never the derived one: the phone posts
            # WAV, so letting `convert_to_wav` swap the suffix would hand
            # ffmpeg its own input file.
            wav = stt.convert_to_wav(src, Path(td) / "voice-16k.wav")
            t = self.transcriber()
            with self._stt_lock:
                return t.transcribe(wav).text.strip()

    # ---- an utterance arriving in pieces ------------------------------
    #: An abandoned utterance — the app was killed mid-sentence, the phone lost
    #: the network — must not hold a transcript forever.
    STREAM_TTL = 300.0

    def stream(self, utt: str):
        """The `stt.Stream` for this utterance, made on first sight."""
        from . import stt
        with self._streams_lock:
            self._sweep_streams()
            found = self._streams.get(utt)
            if found is None:
                found = (time.time(), stt.Stream(self.transcriber()))
            self._streams[utt] = (time.time(), found[1])
            return found[1]

    def push_chunk(self, utt: str, pcm: bytes, *, final: bool) -> str:
        """Transcribe one piece of a live utterance; returns the text so far.

        Serialised on `_stt_lock` with the one-shot path, because it is the
        same model object and faster-whisper does not promise otherwise.
        """
        st = self.stream(utt)
        with self._stt_lock:
            text = st.push(pcm, final=final)
        if final:
            with self._streams_lock:
                self._streams.pop(utt, None)
        return text

    def _sweep_streams(self) -> None:
        cutoff = time.time() - self.STREAM_TTL
        for key in [k for k, (at, _) in self._streams.items() if at < cutoff]:
            self._streams.pop(key, None)

    # ---- an answer leaving in pieces ----------------------------------
    def spool(self, turn: str, text: str) -> "SpeechSpool":
        """Start synthesizing `text` now, to be read later by `/v1/say`.

        Called on the request thread the moment a turn's words exist, so the
        first sentence is already being made while the JSON is still on the
        wire. That overlap is the whole point — see `SpeechSpool`.
        """
        with self._spools_lock:
            cutoff = time.time() - SpeechSpool.TTL
            for key in [k for k, v in self._spools.items() if v.born < cutoff]:
                self._spools.pop(key, None)
            made = SpeechSpool(self, text)
            self._spools[turn] = made
            return made

    def spooled(self, turn: str) -> "SpeechSpool | None":
        with self._spools_lock:
            return self._spools.get(turn)

    def synth(self):
        with self._tts_lock:
            if self._tts is None:
                from . import tts
                m = tts.synthesizer(
                    self.s,
                    on_fallback=lambda why: print(
                        f"phone: hosted voice unavailable, using piper — {why}"))
                m.load()
                self._tts = m
            return self._tts

    def speak(self, text: str) -> bytes:
        """OGG/Opus, because that is what a phone plays without transcoding.

        The lock is held only for a synthesizer that says it needs it. Piper is
        a loaded ONNX session and does need it; the hosted voice is an HTTP call
        whose cost is a fixed round trip, and holding a global lock across that
        is what made spooling an answer sentence-by-sentence *slower overall*
        than synthesizing it in one call — measured, 10.9s against 7.0s for the
        same three sentences. See `tts.RemoteSynthesizer.concurrency`.

        `to_ogg_opus` is outside either branch: it shells out to ffmpeg, which
        is a separate process per call and has never needed serialising.
        """
        from . import tts
        m = self.synth()
        if getattr(m, "concurrency", 1) > 1:
            audio = m.synthesize(text)
        else:
            with self._tts_lock:
                audio = m.synthesize(text)   # MP3 if hosted, WAV if Piper
        return tts.to_ogg_opus(audio)


# ---- speaking the answer before all of it exists --------------------------
class SpeechSpool:
    """One turn's answer, synthesized a sentence at a time, read as it fills.

    The shape this replaces: the phone got the answer as JSON, then called
    `POST /v1/speak`, which synthesized *the whole thing* and returned one blob.
    Measured on Parsa's laptop that was ~4.4s against the hosted voice — a wait
    that started only after V already knew every word it was going to say, and
    that no amount of model choice fixes because it is the wrong shape.

    Here the answer is split by `tts.sentences` and a worker starts on sentence
    one the moment the turn exists — before the phone has even parsed the JSON.
    The phone then reads frames off `GET /v1/say`, which blocks only until the
    *next* sentence is ready rather than until the last one is. Time-to-first-
    word stops scaling with the length of the answer.

    Synthesis stays strictly in order and strictly serial. Out of order would
    need reassembly on a phone for no gain, and parallel would need a
    thread-safe Piper, which `PiperVoice` is not — the `_tts_lock` this takes
    is the same one `_Bridge.speak` uses, for that reason.
    """

    #: A spool is dead weight once its turn has been spoken. Kept briefly
    #: rather than dropped at EOF so a phone that lost the socket mid-answer
    #: can ask again, and swept on the next turn rather than by a timer —
    #: there is one user and turns are the only clock that matters.
    TTL = 300.0

    def __init__(self, bridge: "_Bridge", text: str):
        self.parts: list[bytes] = []
        self.done = threading.Event()
        self.progress = threading.Condition()
        self.error: str = ""
        self.born = time.time()
        self._bridge = bridge
        self._chunks = tts_mod.sentences(for_speech(text))
        if not self._chunks:
            self.done.set()
            return
        # Out of order would need reassembly on a phone for no gain, so results
        # are slotted by index and only ever *published* in order. How many are
        # in flight at once is the synthesizer's call: Piper is one ONNX session
        # and must stay serial, while the hosted voice is a fixed round trip per
        # call and gets slower per sentence unless the trips overlap.
        try:
            width = int(getattr(bridge.synth(), "concurrency", 1))
        except Exception:                   # noqa: BLE001 — no voice loaded yet
            width = 1
        self._slots: list[bytes | None] = [None] * len(self._chunks)
        self._next = 0                      # the lowest index not yet published
        self._claim = 0                     # the lowest index not yet started
        self._lock = threading.Lock()
        workers = max(1, min(width, len(self._chunks)))
        self._running = workers
        for n in range(workers):
            threading.Thread(target=self._run, daemon=True,
                             name=f"v-speech-spool-{n}").start()

    def _run(self) -> None:
        while True:
            with self._lock:
                i = self._claim
                if i >= len(self._chunks):
                    break
                self._claim += 1
            try:
                audio = self._bridge.speak(self._chunks[i])
            except Exception as exc:        # noqa: BLE001 — reported, not raised
                # A voice that fails is not a turn that failed: the words are
                # already on the phone's screen. Recorded so `/v1/say` can end
                # the stream cleanly instead of hanging until the timeout.
                self.error = _scrub(exc)
                audio = b""
            self._publish(i, audio)
        with self.progress:
            self._running -= 1
            if self._running == 0:
                self.done.set()
            self.progress.notify_all()

    def _publish(self, index: int, audio: bytes) -> None:
        """Slot one result and release every frame now contiguous from the
        front — so a sentence that finished early still waits its turn, and
        one that finished late does not hold up the ones behind it twice."""
        with self.progress:
            self._slots[index] = audio
            while (self._next < len(self._slots)
                   and self._slots[self._next] is not None):
                ready = self._slots[self._next]
                if ready:               # a failed sentence is skipped, not sent
                    self.parts.append(ready)
                self._next += 1
            self.progress.notify_all()

    def frames(self, timeout: float = 120.0):
        """Yield each sentence's OGG as it becomes ready, then stop."""
        sent = 0
        deadline = time.time() + timeout
        while True:
            with self.progress:
                while len(self.parts) == sent and not self.done.is_set():
                    if not self.progress.wait(max(0.0, deadline - time.time())):
                        return
                ready = self.parts[sent:]
                sent += len(ready)
            for frame in ready:
                yield frame
            if not ready and self.done.is_set():
                return


# ---- shaping a Response for the wire -------------------------------------
def _buttons(buttons) -> list[dict]:
    out = []
    for b in buttons or []:
        out.append({"label": b.label, "action": b.action,
                    "payload": b.payload or {}})
    return out


def voice_state(resp) -> str:
    """Which of the four colours the app paints ✦, decided here rather than on
    the phone.

    Copied in shape from `cli._voice_state` deliberately: the rule is a product
    rule ("a refusal is never an error", "a degraded answer looks degraded
    whichever path produced it"), and a second implementation of it in Kotlin
    is a second implementation that drifts. The phone renders what it is told.
    """
    if resp.data.get("refused"):
        return "refused"
    if resp.data.get("degraded") or resp.route.endswith(":fallback"):
        return "degraded"
    conf = resp.data.get("confidence")
    if conf is not None and conf < 0.55:
        return "unsure"
    return "speaking"


def for_speech(text: str) -> str:
    """Same cut `telegram._for_speech` makes: drop the citation line, cap it."""
    text = re.split(r"\n\nSources: ", text)[0].strip()
    return text[:SPEAK_LIMIT]


def payload(resp, settings: Settings | None = None) -> dict:
    """The JSON the app renders a turn from.

    Every field the design's cost line needs is here and none of it is
    optional — `◈ route · ⟡ notes · seconds · $cost · ◉ confidence` is
    specified as "dim, never hidden, never behind a flag", so the transport
    that could omit it is the wrong place to make it optional.
    """
    data = resp.data or {}
    return {
        "text": resp.text,
        "spoken": for_speech(resp.text),
        "intent": resp.intent,
        "route": resp.route,
        "decision": resp.decision,
        "citations": list(resp.citations or [])[:12],
        "buttons": _buttons(resp.buttons),
        "state": voice_state(resp),
        "cost": float(resp.cost or 0.0),
        # On a subscription nothing is billed per run, so `cost` stays 0.0 and
        # the allowance consumed travels separately — the app renders it as
        # `≈$0.0507 allowance`, which is the honest form. Calling it spend
        # would be a lie in exactly the place the design says not to tell one.
        "allowance": float(data.get("list_cost_usd") or 0.0),
        "ms": float(resp.ms or 0.0),
        "notes": len(resp.citations or []),
        "confidence": data.get("confidence"),
        "model": data.get("model") or data.get("shell") or "",
        "local": not bool(data.get("list_cost_usd") or resp.cost),
        "refused": bool(data.get("refused")),
        "degraded": bool(data.get("degraded")),
        # Files this turn produced. `data["files"]` is a list of vault-relative
        # paths — that is all a producer knows and all it should have to know —
        # and the name, size and type are resolved here, so the phone can draw
        # a chip without downloading the bytes to find out what they are.
        "files": [file_meta(settings or default_settings, rel)
                  for rel in (data.get("files") or [])
                  if isinstance(rel, str)],
    }


# ---- the home screen -------------------------------------------------------
# How many of each the snapshot carries. Both are "one more than the widest
# widget draws", so a task ticked from the home screen leaves a full list
# behind instead of a gap that waits for the next fetch.
GLANCE_TASKS = 6
GLANCE_EVENTS = 5


def glance(engine, now: dt.datetime | None = None) -> dict:
    """Everything the two home-screen widgets draw, in one free call.

    A widget is not a small chat window. It gets a few hundred milliseconds of
    a process that Android may kill mid-draw, it cannot show a spinner without
    lying, and for most of the day the phone is on a network from which this
    server is simply not reachable. So the contract here is different from
    every other endpoint: one request, everything, no follow-ups — and the
    phone keeps the last answer and draws *that* when the socket is down,
    stamped with when it was true.

    Free by construction. Calendar, tasks and reminders are SQL and Markdown;
    nothing here reaches a model, and `test_httpapi` asserts the cost is 0.0 so
    a later "just summarise the day for the widget" has to argue with a test
    rather than quietly bill a home screen every half hour.

    Times go out as ISO instants and never as "in 42m". The widget on screen is
    a picture taken when it was last redrawn, which may be twenty minutes ago;
    a relative label baked in here would still say "in 42m" long after the
    meeting started, which is worse than saying nothing. The phone computes the
    gap at draw time from the absolute time.
    """
    now = now or dt.datetime.now()
    day = now.date()
    q = engine.q

    events = q.calendar.on(day)
    nxt = q.calendar.next_up(now)
    tasks = q.tasks.priorities(today=day, limit=GLANCE_TASKS)
    shape = q.tasks.backlog_shape(day)
    pending = [r for r in engine.reminders.pending() if r.due.date() <= day]

    counts = {
        "open": shape["open"],
        "overdue": shape["overdue"],
        "due_today": shape["due_today"],
        "events": len(events),
        "left": len([e for e in events if not _past(e, now)]),
        "reminders": len(pending),
        "closed_last_7": shape["closed_last_7"],
    }
    return {
        "at": now.isoformat(timespec="seconds"),
        "day": day.isoformat(),
        "next": _event(nxt, now) if nxt else None,
        "events": [_event(e, now) for e in events[:GLANCE_EVENTS]],
        "tasks": [_task(t, day) for t in tasks],
        "reminders": [{"id": r.id, "text": _plain(r.text, 80),
                       "at": r.due.isoformat(timespec="minutes")}
                      for r in pending[:3]],
        "counts": counts,
        "headline": _headline(counts),
    }


def _past(event, now: dt.datetime) -> bool:
    at = event.starts_at()
    return at is not None and at < now


def _event(event, now: dt.datetime) -> dict:
    at = event.starts_at()
    return {
        "date": event.date.isoformat(),
        "what": _plain(event.summary, 80),
        "where": _plain(event.location or "", 60),
        "when": event.time_label,
        "all_day": event.all_day,
        "at": at.isoformat(timespec="minutes") if at else None,
        "past": _past(event, now),
        # Today or not is a rendering decision — "14:00" alone is a lie on a
        # card that might be showing tomorrow's first meeting because today
        # has none left.
        "today": event.date == now.date(),
    }


# A widget row is one line of ~40 characters. Some tasks in this vault are a
# paragraph with five wikilinks in them (a quarterly key result, written as a
# checkbox), and 500 characters of JSON per row to render 40 of them is waste
# at both ends. Cut generously rather than to the visible width: the card is
# resizable and the font is the phone's, so what fits is not knowable here.
GLANCE_TEXT = 120


def _plain(text: str, limit: int = GLANCE_TEXT) -> str:
    """Wikilinks flattened to their display text, and the line kept short.

    `[[MR-1320 feat(...)|MR-1320]]` is a link on every other surface and noise
    on a home screen, where there is nothing to click through to and no room
    for the brackets. `notes.WIKILINK` already knows the shape, including the
    `#heading` form, so this is the same rule and not a second one.
    """
    from . import notes
    out = notes.WIKILINK.sub(lambda m: m.group(2) or m.group(1), text).strip()
    out = re.sub(r"\s+", " ", out)
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def _task(task, day: dt.date) -> dict:
    age = task.age_days(day) or 0
    return {
        "id": task.id,
        "text": _plain(task.text),
        "where": task.section or task.rel.rsplit("/", 1)[-1][:-3],
        "area": task.area or "",
        "due": task.due.isoformat() if task.due else "",
        "days": age,
        "overdue": task.is_overdue(day),
        "doing": task.state == "doing",
        "carried": task.carried,
    }


def _headline(counts: dict) -> str:
    """The same sentence `briefing.headline` writes, from the same numbers —
    but off `left` rather than `events`, because a widget read at 6pm should
    not still be announcing three meetings that all finished."""
    bits = []
    if counts["left"]:
        bits.append(f"{counts['left']} event{'' if counts['left'] == 1 else 's'}")
    if counts["due_today"]:
        bits.append(f"{counts['due_today']} due")
    if counts["overdue"]:
        bits.append(f"{counts['overdue']} overdue")
    if counts["reminders"]:
        n = counts["reminders"]
        bits.append(f"{n} reminder{'' if n == 1 else 's'}")
    return " · ".join(bits) or "nothing pressing"

# ---- the request handler --------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "V"
    sys_version = ""            # don't advertise the Python version

    # ---- plumbing -----------------------------------------------------
    @property
    def app(self) -> "VServer":
        return self.server.app          # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        if self.app.verbose:
            print(f"  {self.address_string()} {fmt % args}")

    def _peer_cn(self) -> str:
        """The CN of the certificate that completed this handshake.

        Reached only if the handshake succeeded against our CA, so this is not
        the access check — it is which enrolled device is calling, which is
        worth knowing in a log and worth echoing back so a phone can notice it
        is holding a certificate the server has since revoked.
        """
        try:
            cert = self.connection.getpeercert() or {}
        except (AttributeError, ValueError):
            return ""
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    return value
        return ""

    def _body(self, limit: int = 32 * 1024 * 1024) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length")
        if n > limit:
            raise ApiError(413, "too large")
        if not n:
            return b""
        raw = self.rfile.read(n)
        self._read += len(raw)
        return raw

    def _drain(self) -> None:
        """Swallow any request body the handler did not read.

        Keep-alive makes an unread body poison the *next* request on the same
        socket. `BaseHTTPRequestHandler` parses a request line, the handler
        returns without touching `rfile`, and the leftover bytes are still
        sitting there when the parser reads the following request — so a two
        byte `{}` in front of `POST /v1/chat` becomes the request line
        `{}POST /v1/chat`, and the phone is told `Unsupported method
        ('{}POST')` about a request that was perfectly well formed.

        `h_wake` and `h_session_new` take no arguments in the body and never
        read one, while `Api.wake`/`Api.newSession` post `{}` because OkHttp
        requires a body on POST. That pairing was harmless while a wake only
        happened on a headset connecting; the gate calls it far more often, and
        OkHttp pools connections, so it started corrupting the next request.

        Draining here rather than in those two handlers is deliberate: it makes
        the invariant true for every route, including ones not yet written.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = n - self._read
        if remaining > 0:
            self.rfile.read(remaining)

    def _json_body(self) -> dict:
        raw = self._body(limit=1024 * 1024)
        if not raw:
            return {}
        try:
            out = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "body is not JSON")
        if not isinstance(out, dict):
            raise ApiError(400, "body must be a JSON object")
        return out

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-V-Device", self._peer_cn())
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, content_type: str):
        """Open a chunked response and hand back a `write(bytes)`.

        Chunked rather than `Connection: close` — which is what `h_events`
        does — because unlike the event stream this runs once per turn, and
        making the phone redo a TLS handshake for every answer would spend part
        of the latency this endpoint exists to remove.
        """
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-V-Device", self._peer_cn())
        self.end_headers()

        def write(data: bytes) -> None:
            if not data:
                return
            self.wfile.write(f"{len(data):x}\r\n".encode())
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def close() -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        return write, close

    def _json(self, obj: dict, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---- routing ------------------------------------------------------
    #: Bytes of this request's body a handler has consumed, so `_drain` knows
    #: what is left. Reset per request by `do_GET`/`do_POST`.
    _read = 0

    def do_GET(self) -> None:       # noqa: N802 — BaseHTTPRequestHandler's name
        self._dispatch("GET")

    def do_POST(self) -> None:      # noqa: N802
        self._read = 0
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        route = (method, url.path.rstrip("/") or "/")
        query = parse_qs(url.query)
        try:
            cn = self._peer_cn()
            if cn and not self.app.enrolment.allows(cn):
                # Enrolled once, revoked since: the certificate still chains to
                # the CA (there is no CRL, by design) so the handshake succeeds
                # and the check has to be here — against the live set on disk,
                # so a revoke takes effect on the next request rather than on
                # the next restart. Told plainly, because unlike a stranger on
                # Telegram this is a device Parsa owns and needs to know has
                # been cut off.
                raise ApiError(403, "this device is no longer enrolled")
            handler = _ROUTES.get(route)
            if handler is None:
                raise ApiError(404, f"no route for {method} {url.path}")
            handler(self, query)
            self._drain()
        except ApiError as exc:
            self._fail(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass                    # the phone hung up; not worth a line
        except Exception as exc:    # noqa: BLE001 — never leak a traceback
            if self.app.verbose:
                traceback.print_exc()
            self._fail(500, f"{type(exc).__name__}: {_scrub(exc)}")

    def _fail(self, status: int, message: str) -> None:
        try:
            self._json({"error": message, "status": status}, status=status)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---- finishing a turn ---------------------------------------------
    def _speech(self, out: dict, query: dict) -> None:
        """Start synthesizing this answer *now*, if the caller wants it spoken.

        Opt-in on `?speak=1` rather than always: the widgets and the terminal
        call these same endpoints and have no speaker, and warming Piper for
        an answer nobody will hear is work done for nothing.

        The spool is started here — on the request thread, before the JSON is
        written — so sentence one is already being made while the response is
        still travelling. The phone then reads `/v1/say?turn=<id>` and usually
        finds the first frame waiting for it.
        """
        if (query.get("speak") or ["0"])[0] not in ("1", "true", "yes"):
            return
        words = out.get("spoken") or out.get("text") or ""
        if not words.strip():
            return
        turn = uuid.uuid4().hex[:16]
        try:
            self.app.bridge.spool(turn, words)
        except Exception:               # noqa: BLE001
            return                      # no voice is not a failed turn
        out["turn"] = turn

    def _answer(self, text: str, query: dict, *,
                message_type: str = "voice") -> None:
        """Run a turn from a transcript and write the response.

        Shared by `/v1/voice` and `/v1/voice/chunk` so the two paths cannot
        drift: the streaming one is an optimisation of how the audio arrives,
        not a different kind of turn, and every field the app renders — files,
        transcript, the spooled voice — has to be identical either way.
        """
        if not text:
            # Not an error: a held button with nothing said into it is an
            # ordinary thing to do, and an error dialog for it trains you to
            # dismiss dialogs.
            self._json({"transcript": "", "text": "", "spoken": "",
                        "state": "unsure", "empty": True,
                        "buttons": [], "citations": []})
            return

        def _work(engine):
            return (engine.handle(text, message_type=message_type),
                    take_queued_files(engine))

        resp, queued = self.app.bridge.call(_work)
        out = payload(resp, self.app.s)
        out["files"] = out["files"] + queued
        out["transcript"] = text
        self._speech(out, query)
        self._json(out)

    # ---- endpoints ----------------------------------------------------
    def h_hello(self, query: dict) -> None:
        """Everything the app needs to draw its chrome before a word is said."""
        app = self.app

        def _work(engine: Engine) -> dict:
            row = engine.conn.execute("SELECT COUNT(*) FROM notes").fetchone()
            return {"notes": int(row[0]) if row else 0}
        try:
            stats = app.bridge.call(_work, timeout=60)
        except ApiError:
            # The boot line ("412 notes indexed") is chrome, not the product —
            # an engine still cold must not stop the app from opening.
            stats = {}
        self._json({
            "name": "V",
            "api": API_VERSION,
            "device": self._peer_cn(),
            "now": dt.datetime.now().isoformat(timespec="seconds"),
            "timezone": app.s.timezone,
            "tts": app.tts_ready,
            "stt": app.stt_ready,
            "seq": app.bus.seq,
            "listeners": app.bus.listeners,
            **stats,
        })

    def h_glance(self, query: dict) -> None:
        """The home-screen snapshot. See `glance()` for why it is one call.

        Answered on the engine thread like everything else, and with a shorter
        timeout than a chat turn: a widget that waits sixty seconds has already
        been killed by Android, and the phone would rather be told nothing and
        keep drawing what it has.
        """
        snap = self.app.bridge.call(lambda e: glance(e), timeout=20)
        snap["device"] = self._peer_cn()
        self._json(snap)

    def h_chat(self, query: dict) -> None:
        body = self._json_body()
        text = (body.get("text") or "").strip()
        extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
        # An attachment uploaded a moment ago, named by the path `/v1/attach`
        # gave back. Two calls rather than one multipart request, because the
        # caption is the interesting half: it is UTF-8 prose that routes the
        # turn, and putting it in a query string or an HTTP header — the only
        # places a raw-body upload could carry it — would mean latin-1
        # mangling every Persian caption Parsa writes.
        file_rel = (body.get("file_rel") or "").strip()
        if file_rel:
            extra = {**extra, "file_rel": file_rel}
        has_file = bool(body.get("has_file")) or bool(file_rel)
        if not text and not has_file:
            raise ApiError(400, "text is required")
        # A photo with no caption is a complete message — it is how most
        # attachments are actually sent — and `router` already reads
        # `has_file` as enough to route on. Telegram has always allowed it;
        # this endpoint used to reject it at the door.
        mtype = body.get("message_type") or ("document" if has_file else "text")

        def _work(engine):
            resp = engine.handle(text, message_type=mtype,
                                 has_location=bool(body.get("has_location")),
                                 has_file=has_file, extra=extra)
            return resp, take_queued_files(engine)

        resp, queued = self.app.bridge.call(_work)
        out = payload(resp, self.app.s)
        out["files"] = out["files"] + queued
        self._speech(out, query)
        self._json(out)

    def h_attach(self, query: dict) -> None:
        """Bytes in, a vault path out. Say something about it with `/v1/chat`.

        Shaped like `/v1/voice`: the body is the file and nothing else, so
        there is no multipart parser here and no dependency to add one. The
        name rides in the query string, where it is percent-encoded and
        therefore safe for any script; the caption does not, for the reason
        `h_chat` gives.

        The upload is a vault write like any other — `save_binary`, a `[bot]`
        commit, revertible with `v undo`. That is why it happens here and not
        as a temp file the next turn might pick up: a file that is in the vault
        is a file that is backed up, indexed and undoable, and one that is not
        is one more thing to lose.
        """
        name = (query.get("name") or [""])[0].strip() or "file"
        data = self._body(limit=ATTACH_LIMIT + 1)
        if not data:
            raise ApiError(400, "empty upload")
        if len(data) > ATTACH_LIMIT:
            raise ApiError(413, f"{len(data) / 1e6:.1f} MB is over the "
                                f"{ATTACH_LIMIT // 1_000_000} MB attachment "
                                f"limit — every attachment is a git commit in "
                                f"a vault that gets cloned.")
        meta = self.app.bridge.call(
            lambda e: save_attachment(e, data, name), timeout=120)
        self._json(meta)

    def h_file(self, query: dict) -> None:
        """A file out of the vault, by its relative path.

        No privacy-folder check, and that is a decision rather than an
        oversight. `mcp/guard.py` blocks private folders because the thing on
        the other side is a *model*; the thing on the other side of this socket
        is Parsa's own phone, proved by a client certificate this machine
        issued, and `/v1/chat` already answers questions out of those same
        folders. Blocking the file while serving the sentence would be a rule
        that only stops the honest half.

        What *is* enforced is the vault boundary: `vault.path()` resolves and
        refuses anything that climbs out, so `?rel=../../.ssh/id_rsa` is a 400
        and not a key. Losing the phone is answered where it is answerable —
        `pki.revoke`, which this handler is behind like every other.
        """
        rel = (query.get("rel") or [""])[0].strip()
        if not rel:
            raise ApiError(400, "rel is required")
        # Resolved by the engine's own `Vault`, not by a second copy of the
        # rule here. It is pure string arithmetic so it costs nothing to ask
        # the engine thread for it, and one implementation of "inside the
        # vault" is the difference between a boundary and a suggestion.
        try:
            path = self.app.bridge.call(lambda e: e.vault.path(rel), timeout=20)
        except ApiError as exc:
            # `VaultError: path escapes the vault` arrives here as a 500,
            # which is the wrong shape for it: the caller sent a bad path, and
            # a phone told "server error" would retry it forever.
            if exc.status != 500:
                raise
            raise ApiError(400, exc.message) from exc
        except Exception as exc:        # noqa: BLE001
            # Same conclusion by the other route. `_Bridge` wraps whatever the
            # closure raised into an ApiError, but nothing guarantees the
            # object behind `bridge` is that one — and every way of failing to
            # resolve a path is the caller's fault, not this machine's.
            raise ApiError(400, f"bad path: {_scrub(exc)}") from exc
        if not path.exists() or not path.is_file():
            raise ApiError(404, f"no such file: {rel}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ApiError(500, f"could not read {rel}: {exc}")
        # `attachment` and not `inline`: this is a download, and the name is
        # what the phone will file it under. RFC 5987 for the name, because
        # half of them are Persian and a bare `filename=` is latin-1 only.
        quoted = quote(Path(rel).name, safe="")
        self._send(200, data, mime_for(rel), {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        })

    def h_voice(self, query: dict) -> None:
        """Audio in, an answer out. The transcript travels with it.

        The transcript is returned rather than kept server-side because the
        design makes a wrong transcript a *state*, not an error — the phone
        cannot show "this is what I heard, correct me" if it was never told
        what V heard.
        """
        data = self._body()
        if not data:
            raise ApiError(400, "empty audio body")
        suffix = _suffix(self.headers.get("Content-Type", ""))
        try:
            transcript = self.app.bridge.transcribe(data, suffix)
        except ImportError as exc:
            raise ApiError(503, f"no speech engine installed: {_scrub(exc)}")
        except Exception as exc:    # noqa: BLE001
            # Distinguished from the line above because they need different
            # things done about them, and collapsing both into "no speech
            # engine available" is how a one-line ffmpeg bug spent an evening
            # looking like a missing Whisper install. Anything reaching here
            # is a fault in a component that *is* present.
            from . import stt
            if isinstance(exc, stt.Unavailable):
                raise ApiError(503, f"no speech engine available: {_scrub(exc)}")
            detail = getattr(exc, "stderr", b"") or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise ApiError(500, f"transcription failed: {_scrub(exc)}"
                                f"{' — ' + detail.strip()[:200] if detail else ''}")
        self._answer(transcript, query)

    def h_voice_chunk(self, query: dict) -> None:
        """One piece of an utterance that is still being spoken.

        The phone posts raw PCM16LE at 16kHz — what `AudioRecord` already hands
        it — cut at a silence its VAD found. Everything but the last piece has
        been through Whisper before the finger leaves the button, so the wait
        at the end is one pass over the tail rather than one pass over all of
        it. See `stt.Stream` for why the cuts have to be at silences and why
        short utterances deliberately stay a single pass.

        Non-final chunks return only the transcript so far. The final one runs
        the turn, so the answer comes back on the same request that ended the
        sentence — no extra round trip to start thinking.
        """
        utt = (query.get("utt") or [""])[0].strip()
        if not utt or len(utt) > 64 or not utt.replace("-", "").isalnum():
            raise ApiError(400, "utt must be a short alphanumeric id")
        final = (query.get("final") or ["0"])[0] in ("1", "true", "yes")
        pcm = self._body(limit=8 * 1024 * 1024)
        if not pcm and not final:
            raise ApiError(400, "empty chunk")
        try:
            transcript = self.app.bridge.push_chunk(utt, pcm, final=final)
        except ImportError as exc:
            raise ApiError(503, f"no speech engine installed: {_scrub(exc)}")
        except Exception as exc:            # noqa: BLE001
            from . import stt
            if isinstance(exc, stt.Unavailable):
                raise ApiError(503, f"no speech engine available: {_scrub(exc)}")
            raise ApiError(500, f"transcription failed: {_scrub(exc)}")

        if not final:
            self._json({"transcript": transcript, "final": False})
            return
        self._answer(transcript, query)

    def h_say(self, query: dict) -> None:
        """The answer, one sentence of OGG at a time, as they are made.

        Frames are length-prefixed rather than concatenated: OGG pages are
        self-delimiting in theory and a phone would still have to demux them to
        know when to hand one to a `MediaPlayer`, so four bytes of length is
        both simpler and what the player actually wants.
        """
        turn = (query.get("turn") or [""])[0].strip()
        spool = self.app.bridge.spooled(turn) if turn else None
        if spool is None:
            raise ApiError(404, "no such turn, or its audio has expired")
        write, close = self._stream("application/octet-stream")
        try:
            for frame in spool.frames():
                write(len(frame).to_bytes(4, "big") + frame)
            close()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                # he hung up, or hushed it; not worth a line

    def h_speak(self, query: dict) -> None:
        body = self._json_body()
        text = for_speech(body.get("text") or "")
        if not text:
            raise ApiError(400, "text is required")
        try:
            ogg = self.app.bridge.speak(text)
        except Exception as exc:    # noqa: BLE001
            # Degrade rather than fail the turn, the same posture `_send_voice`
            # takes on Telegram: the words are already on screen.
            raise ApiError(503, f"no voice available: {_scrub(exc)}")
        self._send(200, ogg, "audio/ogg")

    def h_act(self, query: dict) -> None:
        body = self._json_body()
        action = (body.get("action") or "").strip()
        if not action:
            raise ApiError(400, "action is required")
        pay = body.get("payload") if isinstance(body.get("payload"), dict) else {}

        def _work(engine):
            return engine.act(action, pay), take_queued_files(engine)

        resp, queued = self.app.bridge.call(_work)
        out = payload(resp, self.app.s)
        out["files"] = out["files"] + queued
        self._speech(out, query)
        self._json(out)

    def h_session_new(self, query: dict) -> None:
        def _work(engine: Engine) -> dict:
            engine.end_session("new")
            engine.start_session()
            return {"ok": True}
        self._json(self.app.bridge.call(_work, timeout=120))

    def h_wake(self, query: dict) -> None:
        """The wake word matched: is anything worth saying before he speaks?

        Deliberately cheap and deliberately quiet. It runs no model and asks no
        question — it answers the design's WAKE HEARD state ("Yes?") and adds
        one line only when there is something time-bound to add. A wake that
        always recites the day would make saying "Friday" something you learn
        to talk over, which is the same failure the briefing's thin-signal
        guard exists to prevent.

        `agent` names who was summoned. The phone's gate is trained on one
        wake word and that word is a *name*, so which name was heard decides
        who answers — this is the seam where "Friday" stops being a trigger
        token and becomes the persona with her own memory scope. The switch
        goes through `Engine.switch_agent` rather than assigning `persona`
        directly, because that is what rebuilds the memory store on the new
        scope and clears the previous agent's shell thread; setting the field
        would give Friday V's beliefs and V's transcript.

        An unreachable or unknown agent is not an error. `switch_agent`
        already refuses and says why, and a wake that fails because a config
        names something odd would be a wake that silently stops working.
        """
        wanted = (query.get("agent") or [""])[0].strip()

        def _work(engine: Engine) -> dict:
            if wanted:
                try:
                    engine.switch_agent(wanted)
                except Exception:
                    pass    # V answers, which is the documented fallback
            now = dt.datetime.now()
            bits: list[str] = []
            try:
                nxt = engine.agenda.next_event(now)
                if nxt is not None:
                    bits.append(nxt.render())
            except Exception:
                pass
            try:
                due = engine.reminders.due_now(now)
                for r in due[:2]:
                    bits.append(r.describe())
            except Exception:
                pass
            return {"lines": bits, "agent": engine.persona.key}

        out = self.app.bridge.call(_work, timeout=90)
        lines = out.get("lines") or []
        text = "Yes?" if not lines else "Yes? " + " ".join(lines)
        self._json({
            "text": text,
            "spoken": text,
            "state": "speaking",
            "lines": lines,
            "agent": out.get("agent", "v"),
            "source": (query.get("source") or ["unknown"])[0],
            "cost": 0.0, "allowance": 0.0, "local": True,
            "buttons": [], "citations": [],
        })

    def h_events(self, query: dict) -> None:
        """Server-sent events: the proactive channel, held open by the phone.

        SSE rather than a websocket or a push service. A websocket is a second
        framing protocol for a stream that only ever flows one way, and a push
        service (FCM) would route a reminder about a dentist appointment
        through Google to arrive on a phone that is already holding an
        authenticated socket to the machine that generated it — the exact
        third party this transport exists to remove.
        """
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        bus = self.app.bus
        cn = self._peer_cn()
        q = bus.subscribe(since)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-V-Device", cn)
        # No Content-Length is possible on a stream, so the connection cannot
        # be reused afterwards and must be closed rather than left half-parsed.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            while not self.app.stopping:
                try:
                    event = q.get(timeout=20)
                except queue.Empty:
                    event = None
                # Revocation has to reach a connection that is already open.
                # Every other endpoint is checked on the way in and answers in
                # seconds; this one is held open for hours by design, so a
                # phone revoked at 11am would otherwise go on being read every
                # reminder and every nudge until it lost signal. Checked here,
                # after the wait and before the write, so the revoked device
                # does not get one last event through the gap — and so an idle
                # stream still closes within a keepalive of the revoke rather
                # than waiting for news that may never come. Costs one stat
                # per event or per 20s — see `Enrolment`.
                if cn and not self.app.enrolment.allows(cn):
                    break
                if event is None:
                    # A comment line, not an event. Android drops an idle TCP
                    # connection well before any server would, and a stream
                    # that only speaks when there is news is a stream that is
                    # dead by the time there is.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                frame = (f"id: {event['seq']}\n"
                         f"event: {event['kind']}\n"
                         f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ssl.SSLError):
            pass
        finally:
            bus.unsubscribe(q)


def _suffix(content_type: str) -> str:
    """faster-whisper reads what ffmpeg can convert, and ffmpeg goes by the
    extension when the container is ambiguous. Android records `audio/mp4`
    (AAC) by default and OGG/Opus when asked; both are handled."""
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/webm": ".webm",
        "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/3gpp": ".3gp",
    }.get(ct, ".ogg")


_ROUTES = {
    ("GET", "/v1/hello"): _Handler.h_hello,
    ("GET", "/v1/glance"): _Handler.h_glance,
    ("POST", "/v1/chat"): _Handler.h_chat,
    ("POST", "/v1/attach"): _Handler.h_attach,
    ("GET", "/v1/file"): _Handler.h_file,
    ("POST", "/v1/voice"): _Handler.h_voice,
    ("POST", "/v1/voice/chunk"): _Handler.h_voice_chunk,
    ("GET", "/v1/say"): _Handler.h_say,
    ("POST", "/v1/speak"): _Handler.h_speak,
    ("POST", "/v1/act"): _Handler.h_act,
    ("POST", "/v1/session/new"): _Handler.h_session_new,
    ("POST", "/v1/wake"): _Handler.h_wake,
    ("GET", "/v1/events"): _Handler.h_events,
}


# ---- the listener ---------------------------------------------------------
class _TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        """Wrap each accepted socket, and let a failed handshake vanish.

        `socketserver` catches `OSError` from here and simply moves on, and
        `ssl.SSLError` is one — which is exactly the behaviour wanted: a client
        with no certificate, or one signed by anything but our CA, gets a
        closed connection and no message. Telling it *why* it was rejected
        would confirm to a scanner that something worth scanning is here.

        The timeout is on the handshake alone. Without it a client that opens a
        socket and says nothing holds the accept loop for as long as it likes.
        """
        sock, addr = super().get_request()
        sock.settimeout(15)
        try:
            wrapped = self.ssl_context.wrap_socket(sock, server_side=True)
        except BaseException:
            try:
                sock.close()
            finally:
                raise
        wrapped.settimeout(None)
        return wrapped, addr


class VServer:
    """The whole surface: TLS, the bus, the engine bridge, the scheduler."""

    def __init__(self, settings: Settings | None = None, *,
                 bind: str | None = None, port: int | None = None,
                 scheduler: bool = True, verbose: bool = False):
        self.s = settings or default_settings
        self.binds = [bind] if bind else resolve_binds(self.s)
        # Kept for anything that wants one name for the listener — the status
        # line, the log, the address handed to the app.
        self.bind = self.binds[0] if bind else resolve_bind(self.s)
        self.port = int(port or self.s.phone_port)
        self.verbose = verbose
        self.bus = Broadcast()
        # Built in `serve_forever`, not here. Constructing a `_Bridge` starts a
        # thread that opens the index, re-checks every note's mtime and may
        # write to `state/` — real work, and wrong to do as a side effect of
        # making an object. It also meant a server that *refuses* to start
        # (nothing enrolled) had already opened a database before saying so.
        self.bridge: _Bridge | None = None
        self.enrolment = Enrolment(self.s)
        self.stopping = False
        self._scheduler = scheduler
        self._httpds: list[_TLSServer] = []
        self.tts_ready = False
        self.stt_ready = False

    @property
    def devices(self) -> frozenset[str]:
        """Who may connect *right now* — read off disk, never a boot snapshot.
        See `Enrolment` for why that distinction is the whole point."""
        return self.enrolment.names

    # ---- TLS ----------------------------------------------------------
    def context(self) -> ssl.SSLContext:
        ca, _ = pki.ensure_ca(self.s)
        crt, key = pki.ensure_server(self.s)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # 1.2 is the floor rather than 1.3 only because a phone that has been
        # sitting in a drawer through an OS version is still a phone Parsa
        # expects to work; both are modern enough that nothing here is at risk.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED     # the access control, entire
        ctx.load_verify_locations(cafile=str(ca))
        ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
        return ctx

    # ---- warm-up ------------------------------------------------------
    def _warm(self) -> None:
        """Load Whisper and Piper before the first message rather than during
        it. Both are multi-second cold loads, and paying that inside the first
        voice reply is how a working system reads as a broken one."""
        def _run() -> None:
            try:
                self.bridge.transcriber()
                self.stt_ready = True
            except Exception as exc:    # noqa: BLE001
                print(f"  stt unavailable — voice in is off: {_scrub(exc)}")
            try:
                self.bridge.synth()
                self.tts_ready = True
            except Exception as exc:    # noqa: BLE001
                print(f"  tts unavailable — replies will be text only: "
                      f"{_scrub(exc)}")
        threading.Thread(target=_run, daemon=True, name="v-http-warm").start()

    def _start_scheduler(self) -> None:
        from .scheduler import Scheduler

        def _run() -> None:
            # Its own engine, on its own thread — same rule as everywhere else.
            engine = Engine(self.s, surface=persona_mod.PHONE)
            try:
                Scheduler(engine, self.s,
                          sink=build_sink(self.bus, self.s)).run(interval=30)
            except Exception as exc:    # noqa: BLE001
                print(f"  scheduler stopped: {_scrub(exc)}")
            finally:
                engine.close()
        threading.Thread(target=_run, daemon=True, name="v-http-scheduler").start()

    # ---- run ----------------------------------------------------------
    def serve_forever(self) -> None:
        if not self.devices:
            raise ApiError(
                500, "no device is enrolled — run `cli phone enrol <name>` "
                     "first, or nothing will be able to connect")
        # Tests inject a stub in place of this; a real run builds it here, once
        # the enrolment check above has decided there is any point.
        if self.bridge is None:
            self.bridge = _Bridge(self.s)
        ctx = self.context()
        opened: list[str] = []
        for address in self.binds:
            try:
                httpd = _TLSServer((address, self.port), _Handler)
            except OSError as exc:
                # One address failing is not the run failing: a tailnet
                # interface that has not come up yet, or a lease that changed
                # since the certificate was issued, must not take the LAN
                # listener down with it.
                print(f"  could not listen on {address}: {_scrub(exc)}")
                continue
            httpd.ssl_context = ctx         # type: ignore[attr-defined]
            httpd.app = self                # type: ignore[attr-defined]
            self._httpds.append(httpd)
            opened.append(address)
        if not self._httpds:
            raise ApiError(500, f"could not listen on any of "
                                f"{', '.join(self.binds)}:{self.port}")

        self._warm()
        if self._scheduler:
            self._start_scheduler()
        tail = pki.tailnet_ip()
        for address in opened:
            label = "  tailnet " if address == tail else "  local   "
            print(f"{label} https://{address}:{self.port}")
        print(f"  devices: {', '.join(sorted(self.devices))}")

        threads = [threading.Thread(target=h.serve_forever, kwargs={"poll_interval": 0.5},
                                    daemon=True, name=f"v-http-{a}")
                   for h, a in zip(self._httpds[1:], opened[1:])]
        for t in threads:
            t.start()
        try:
            self._httpds[0].serve_forever(poll_interval=0.5)
        finally:
            self.stopping = True
            for httpd in self._httpds:
                try:
                    httpd.server_close()
                except OSError:
                    pass
            self.bridge.stop()

    def stop(self) -> None:
        self.stopping = True
        for httpd in self._httpds:
            threading.Thread(target=httpd.shutdown, daemon=True).start()


def resolve_binds(s: Settings | None = None) -> list[str]:
    """Every address to listen on — one socket each, never a wildcard.

    `0.0.0.0` is not the answer, even though it would be one line instead of
    this function. mTLS means a hostile client cannot get a request through,
    but it can still reach the port, and a port that answers is a port worth
    attacking; a wildcard bind also quietly publishes V to every docker bridge
    on the box. Naming the interfaces keeps the reachable surface equal to the
    set of addresses the certificate was actually issued for.

    **Both, not one.** The first version picked the tailnet address if there
    was one and the LAN address otherwise — which is exactly wrong for a phone
    that toggles its VPN. Android permits one VPN at a time, so a work VPN and
    Tailscale cannot both be up; the phone therefore arrives over the tailnet
    sometimes and over the LAN the rest of the time, and a server listening on
    whichever address it preferred at start-up is unreachable for half of that.
    Two sockets cost two threads.
    """
    s = s or default_settings
    if s.phone_bind:
        return [h.strip() for h in s.phone_bind.split(",") if h.strip()]
    hosts = [h for h in pki.local_hosts() if not h.startswith("127.")]
    return hosts or ["127.0.0.1"]


def resolve_bind(s: Settings | None = None) -> str:
    """The primary address — what `phone status` prints and what the app is
    told to dial first. The tailnet wins when there is one, because it is the
    address that keeps working when Parsa leaves the house."""
    binds = resolve_binds(s)
    tail = pki.tailnet_ip()
    if tail and tail in binds:
        return tail
    return binds[0]


def run(settings: Settings | None = None, **kw) -> None:
    VServer(settings, **kw).serve_forever()
