"""Telegram — the transport. Nothing above this file knows Telegram exists.

`Engine.handle(text) -> Response` is the whole contract; this is the shim the
README promised: turn an update into a call to `handle()` (or `act()` for a
button press) and turn the `Response` back into a message, `Response.buttons`
rendered as an inline keyboard.

No python-telegram-bot, no aiogram. The Bot API is JSON over HTTPS and long
polling is one GET in a loop — the same shape this vault already chose for
Google Calendar (`gcal.py`) over `google-api-python-client`: the dependency
buys nothing a small HTTP client doesn't already have, and it's one more
thing to vet for the folders in `private_folders` never to reach.

Three things live here that don't belong anywhere else either:

**Owner-only, enforced at the door.** There is exactly one user everywhere
else in this codebase, but a bot token is reachable by anyone who finds the
bot's username — nothing upstream of this module assumes that, so it has to
be checked here. The first private message the bot ever receives claims
ownership (written to `state/config.json`, the same file `cli shell --use`
already writes to); every message from anyone else, in any chat, is dropped
before it reaches the engine — no reply, so a stranger who finds the bot
learns nothing from it.

**Archive groups read everything, unasked.** A group whose only human member
is Parsa isn't a conversation — it's a place he forwards music and links to
himself, so there is no "hey V" to address and no router to run. Messages
there go straight to a dated log note per chat, with any file downloaded and
embedded, and get a 👍 reaction rather than a reply — a message back into a
group with no one to read it would be V talking to itself.

**Downloads and transcription never sit in front of the message queue.**
The first cut of this ran everything — fetch, transcribe, `engine.handle` —
on one serial worker, on the theory that only vault writes needed to be
serial. That theory was wrong once a first-time Whisper download landed in
the middle of it: a single voice memo blocked every message behind it,
`/new` included, for as long as a multi-gigabyte download took, because nothing
downstream of the queue could run until upstream of it finished. `_prepare`
is the fix — it does every slow, engine-free thing (download, transcode,
transcribe) on a small thread pool, and only the fast, order-sensitive part
(the vault write, `engine.handle`) still runs on the one thread that owns the
`Engine`. See `_commit`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import json
import os
import logging
import queue
import re
import signal
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

from .briefing import _plain
from .config import Settings, settings as default_settings
from . import persona as persona_mod
from .engine import Engine

# `httpx/_client.py` logs `'HTTP Request: %s %s "%s %d %s"'` at INFO for
# every call — full URL included — through `logging.getLogger("httpx")`.
# Silent by default (no handler on that logger, nothing on the root either),
# until something else in the process attaches one — which, confirmed on
# this exact run, is what `faster_whisper`'s model loader does globally as a
# side effect the first time it touches the network. From that moment every
# `httpx` call anywhere in the process starts printing, and every Telegram
# Bot API URL — `api.telegram.org/bot<TOKEN>/<method>`, and doubly the file-
# download endpoint — carries the token *in the URL*, so that log line is the
# token, verbatim, headed for whatever's capturing stdout. Set here, at
# import time, so it's in effect before any thread in this module makes its
# first request — the fix has to sit above the trigger, not react to it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"

# Bounded, not unlimited: four attachments transcoding/downloading at once is
# plenty of parallelism for a single-user bot, and an unbounded pool would let
# a burst of forwarded videos exhaust memory before the serial worker gets a
# chance to drain any of them.
_PREP_WORKERS = 4


class TelegramError(RuntimeError):
    pass


_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")


def _scrub(text: str) -> str:
    """Redact a bot token out of anything about to be printed.

    The token lives *in the URL* for every Telegram Bot API call — both
    `api.telegram.org/bot<TOKEN>/<method>` and the file-download endpoint —
    and `httpx`'s own exception messages embed the full request URL
    (confirmed: `HTTPStatusError.__str__` includes it). A caught network
    error is exactly the kind of thing that ends up in a log file or a
    terminal someone screenshots, so nothing gets printed without going
    through here first.
    """
    return _TOKEN_RE.sub("bot***REDACTED***", str(text))


def _read_config(s: Settings) -> dict:
    path = s.state_dir / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_config(s: Settings, raw: dict) -> None:
    """The one place `state/config.json` gets written — it holds the bot
    token, so every write locks it to owner-only (0600) rather than leaving
    it at the process umask's default, which on this machine was
    world-readable (0664): any other local account could have read the token
    straight off disk."""
    path = s.state_dir / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _enabled(s: Settings) -> bool:
    # BRAIN_NO_TELEGRAM mirrors BRAIN_NO_NOTIFY — the test suite builds real
    # Engines and really runs `briefing.run`, and that must not mean a real
    # HTTP call to a real bot token sitting in a developer's state/config.json.
    return bool(s.telegram_bot_token) and not os.environ.get("BRAIN_NO_TELEGRAM")


def _tts_enabled(s: Settings) -> bool:
    # BRAIN_NO_TTS mirrors BRAIN_NO_TELEGRAM — the test suite drives real
    # `_commit_converse` calls and that must not mean spinning up Piper.
    return s.tts_provider != "null" and not os.environ.get("BRAIN_NO_TTS")


def _call(s: Settings, method: str, **params) -> dict:
    if not s.telegram_bot_token:
        raise TelegramError("no telegram_bot_token configured")
    r = httpx.post(API.format(token=s.telegram_bot_token, method=method),
                   json=params, timeout=40)
    try:
        data = r.json()
    except ValueError as exc:
        raise TelegramError(f"bad response: {r.text[:200]}") from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description", "unknown error"))
    return data["result"]


# ---- outbound: the proactive half -------------------------------------
def _to_telegram(text: str) -> str:
    """Markdown built for Obsidian → something honest in a chat bubble.

    Reuses the conversion `notify-send` already needed: a bubble has no
    markdown engine either, and Telegram's own parse modes are strict enough
    (MarkdownV2 requires escaping half of ASCII) that round-tripping V's
    CommonMark through one is more likely to 400 the request than to render a
    bold word. Plain text always sends.
    """
    return _plain(text)


# Buttons: Telegram caps callback_data at 64 bytes, and some of V's payloads
# (an evening draft, a full capture text) run far past that. Rather than
# shrink the payload, keep it server-side — the token is what travels.
_CALLBACKS: dict[str, tuple[str, dict]] = {}
_CALLBACK_CAP = 500


def _remember(action: str, payload: dict) -> str:
    token = uuid.uuid4().hex[:16]
    _CALLBACKS[token] = (action, payload or {})
    if len(_CALLBACKS) > _CALLBACK_CAP:
        for k in list(_CALLBACKS)[: len(_CALLBACKS) - _CALLBACK_CAP]:
            _CALLBACKS.pop(k, None)
    return token


def _keyboard(buttons: list) -> list[list[dict]]:
    return [[{"text": b.label[:60], "callback_data": _remember(b.action, b.payload)}]
            for b in buttons]


def send_message(s: Settings, chat_id: int, text: str, *,
                  buttons: list | None = None) -> bool:
    """Best-effort — never raises. A failed push has to degrade, not crash
    the scheduler tick or the cron-driven briefing that called it."""
    if not _enabled(s) or not chat_id or not text.strip():
        return False
    kw = {"chat_id": chat_id, "text": _to_telegram(text)[:4096],
          "disable_web_page_preview": True}
    if buttons:
        kw["reply_markup"] = {"inline_keyboard": _keyboard(buttons)}
    try:
        _call(s, "sendMessage", **kw)
        return True
    except (TelegramError, httpx.HTTPError):
        return False


def notify_owner(s: Settings | None = None, text: str = "") -> bool:
    s = s or default_settings
    return send_message(s, s.telegram_owner_id, text)


def _send_voice_file(s: Settings, chat_id: int, ogg_bytes: bytes) -> None:
    """`sendVoice` — unlike every other call in this module, this one is a
    multipart upload, not JSON, so it can't go through `_call`."""
    if not s.telegram_bot_token:
        raise TelegramError("no telegram_bot_token configured")
    url = API.format(token=s.telegram_bot_token, method="sendVoice")
    r = httpx.post(url, data={"chat_id": chat_id},
                   files={"voice": ("voice.ogg", ogg_bytes, "audio/ogg")},
                   timeout=60)
    try:
        data = r.json()
    except ValueError as exc:
        raise TelegramError(f"bad response: {r.text[:200]}") from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description", "unknown error"))


_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _send_multipart_file(s: Settings, chat_id: int, method: str, field: str,
                         data: bytes, filename: str, caption: str = "") -> None:
    """The shared body of `sendDocument`/`sendPhoto` — multipart uploads,
    same reason `_send_voice_file` above can't go through `_call`."""
    if not s.telegram_bot_token:
        raise TelegramError("no telegram_bot_token configured")
    url = API.format(token=s.telegram_bot_token, method=method)
    form = {"chat_id": chat_id}
    if caption:
        form["caption"] = caption[:1024]
    r = httpx.post(url, data=form, files={field: (filename, data)}, timeout=60)
    try:
        resp = r.json()
    except ValueError as exc:
        raise TelegramError(f"bad response: {r.text[:200]}") from exc
    if not resp.get("ok"):
        raise TelegramError(resp.get("description", "unknown error"))


def send_file(s: Settings, path: Path, *, caption: str = "",
             chat_id: int | None = None) -> bool:
    """Best-effort, same contract as `send_message`: push a file — a
    rendered chart's HTML, a screenshot, a PDF — to the owner's chat.

    This exists because `render.py`'s `open_file()` opens a browser tab on
    whatever machine happens to be running the MCP server, which is exactly
    nothing when the conversation is happening from Parsa's phone over
    Telegram rather than at that machine's keyboard — the tab opens where
    nobody is looking. Callers keep calling `open_file()` too (it's free and
    still right for the local-CLI case); this is the other half, for when
    the transport is Telegram instead of a terminal in front of a screen.

    Images get Telegram's own inline preview (`sendPhoto`); everything else
    — HTML, PDF, whatever — is a downloadable `sendDocument`. Silent no-op,
    like `send_message`, when no bot is configured or the file is missing:
    a chart must not fail to render just because Telegram isn't set up.
    """
    if not _enabled(s):
        return False
    chat_id = chat_id or s.telegram_owner_id
    if not chat_id or not path.exists():
        return False
    try:
        data = path.read_bytes()
        if path.suffix.lower() in _PHOTO_EXTS:
            _send_multipart_file(s, chat_id, "sendPhoto", "photo",
                                 data, path.name, caption)
        else:
            _send_multipart_file(s, chat_id, "sendDocument", "document",
                                 data, path.name, caption)
        return True
    except (TelegramError, httpx.HTTPError, OSError):
        return False


def _for_speech(text: str) -> str:
    """A chat bubble's text isn't always nice to hear out loud. Strips the
    `Sources: url url` line `_response_text` appends — a list of vault
    links read aloud is noise, not information — and caps length so a very
    long answer doesn't turn into a multi-minute voice note nobody asked
    for; the full text is always in the bubble already."""
    text = re.split(r"\n\nSources: ", text)[0].strip()
    return text[:2000]


def _response_text(resp) -> str:
    """`Response.render()` appends a bracketed `[label] [label]` list of its
    buttons — the CLI's own way of showing an affordance it can't actually
    click. Telegram gets real inline buttons, so that text would be a second,
    uglier copy of the same choice sitting right above the real one."""
    out = resp.text
    if resp.citations:
        out += "\n\nSources: " + " ".join(resp.citations[:6])
    return out


def build_sink(s: Settings):
    """A `Scheduler(..., sink=...)` that pushes to Telegram instead of stdout.

    Reminders, brag drafts, the morning brief — every job already hands the
    scheduler something with a `.render()` (a `Response`) or a plain string,
    same as `Scheduler._default_sink` prints. This one just changes where the
    text goes, same contract, no job needs to know the difference.
    """
    def sink(source: str, item) -> None:
        name, payload = item if isinstance(item, tuple) else (source, item)
        items = payload if isinstance(payload, list) else [payload]
        for p in items:
            if p is None:
                continue
            text = getattr(p, "render", lambda: str(p))()
            if not text or not text.strip():
                continue
            send_message(s, s.telegram_owner_id, text,
                        buttons=getattr(p, "buttons", None))
    return sink


def _safe_end_session(engine: Engine) -> None:
    try:
        engine.end_session("interrupted")
    except Exception:
        pass


# ---- filenames ----------------------------------------------------------
def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\s؀-ۿ-]", "", name or "").strip()
    return re.sub(r"\s+", " ", name)[:60]


# `/start` is Telegram's own convention — sent automatically the first time
# someone opens a chat with a bot, or taps its Start button — not a sentence
# Parsa composed. Routing it through `engine.handle` reads it as an ordinary
# question with nothing in it, which is exactly the shape that used to fall
# through to the agent and burn a turn on "That needs more than a lookup —
# several things at once" for a button press that meant nothing at all.
_IGNORED_COMMANDS = {"/start"}


# ---- inbound: the conversational half -----------------------------------
class TelegramBot:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        self._offset_path = self.s.state_dir / "telegram_offset.json"
        # Prepared items land here, ready for the one thread that owns the
        # `Engine` to commit in milliseconds — see `_worker`/`_commit`. A
        # queue rather than a thread per commit: two messages writing the
        # same vault note at once (two quick captures, two photos to the same
        # archive group) would race on the read-modify-write `append` does.
        self._queue: "queue.Queue[dict]" = queue.Queue()
        # One shared transcriber, not one per call. A separate `Transcriber`
        # instance per voice message used to mean a separate `load()` per
        # message too — on this run that showed up as *two* concurrent
        # multi-gigabyte downloads of the same model racing each other, one
        # from the warm-up thread and one from the first real voice memo,
        # neither able to see the other had already started. `_stt_lock`
        # serialises both loading it once and running inference on it, since
        # ctranslate2's thread-safety for concurrent `.transcribe()` calls on
        # one model isn't guaranteed — a lost bit of parallelism between two
        # voice memos arriving at the same instant, which is rare, in
        # exchange for never corrupting a transcription, which would not be.
        self._stt = None
        self._stt_lock = threading.Lock()
        # Same reasoning as `_stt`/`_stt_lock` above, mirrored for the voice
        # going the other way: one shared synthesizer, loaded once, calls
        # serialised through the lock rather than trusting Piper's ONNX
        # session to be safe under concurrent `synthesize_wav` calls. The
        # hosted voice is a plain HTTP request and needs no such protection,
        # but it falls back to Piper, so the lock has to cover both.
        self._tts = None
        self._tts_lock = threading.Lock()

    # ---- owner, claimed once and enforced on every update -------------
    def _claim(self, user_id: int) -> None:
        self.s.telegram_owner_id = user_id
        raw = _read_config(self.s)
        raw["telegram_owner_id"] = user_id
        _write_config(self.s, raw)

    def _is_owner(self, user_id: int | None) -> bool:
        return bool(self.s.telegram_owner_id) and user_id == self.s.telegram_owner_id

    # ---- feel: "it's working on it", not silence ------------------------
    @contextlib.contextmanager
    def _chat_action(self, chat_id: int, action: str):
        """Telegram shows `action` (a "typing…" / "recording voice…" hint
        in the chat header) for about 5 seconds per call. Anything V does
        that takes longer than that — an escalation to the model, a Piper
        synthesis — needs it re-sent on a timer or the hint just vanishes
        partway through, which reads as the bot having gone quiet rather
        than as it still being on the job."""
        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                try:
                    _call(self.s, "sendChatAction", chat_id=chat_id, action=action)
                except (TelegramError, httpx.HTTPError):
                    pass
                stop.wait(4)

        t = threading.Thread(target=_loop, daemon=True, name="telegram-chat-action")
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=1)

    def _reply(self, chat_id: int, text: str, *, buttons: list | None = None) -> None:
        """The one place a direct reply leaves this process: text always,
        then a voice note of the same words — Parsa's explicit ask, so
        it's unconditional here rather than a per-call choice each `_commit_*`
        has to remember to make."""
        send_message(self.s, chat_id, text, buttons=buttons)
        self._send_voice(chat_id, text)

    def _send_voice(self, chat_id: int, text: str) -> None:
        if not _tts_enabled(self.s):
            return
        spoken = _for_speech(text)
        if not spoken:
            return
        from . import tts
        with self._chat_action(chat_id, "record_voice"):
            try:
                # MP3 from the hosted voice, WAV from Piper; `to_ogg_opus`
                # takes either, since ffmpeg sniffs its input.
                audio = self._synth().synthesize(spoken)
                ogg = tts.to_ogg_opus(audio)
            except tts.Unavailable:
                return  # no model installed — degrade to text-only, silently
            except Exception as exc:
                print(f"telegram: tts synthesis failed: "
                     f"{type(exc).__name__}: {_scrub(exc)}")
                return
        try:
            _send_voice_file(self.s, chat_id, ogg)
        except (TelegramError, httpx.HTTPError) as exc:
            print(f"telegram: sendVoice failed: {_scrub(exc)}")

    # ---- long polling ---------------------------------------------------
    def _offset(self) -> int:
        try:
            return json.loads(self._offset_path.read_text(encoding="utf-8"))["offset"]
        except (OSError, json.JSONDecodeError, KeyError):
            return 0

    def _save_offset(self, n: int) -> None:
        try:
            self._offset_path.write_text(json.dumps({"offset": n}), encoding="utf-8")
        except OSError:
            pass

    def run(self) -> None:
        if not self.s.telegram_bot_token:
            raise TelegramError(
                "telegram_bot_token is not set — export TELEGRAM_BOT_TOKEN or "
                "add it to " + str(self.s.state_dir / "config.json"))
        print("telegram bot polling · owner "
             + ("claimed" if self.s.telegram_owner_id
                else "UNCLAIMED — first message to it in a DM wins ownership"))
        # The single serial committer: `engine.handle`, vault writes, and
        # `send_message` all happen here, off the polling thread, so a slow
        # one of those never stops `getUpdates` from running.
        threading.Thread(target=self._worker, daemon=True,
                         name="telegram-worker").start()
        # Warm the transcription model once, off to the side, so the first
        # real voice message of a run doesn't pay for a multi-GB download
        # while someone is sitting there waiting on a reply. Harmless if
        # nothing ever sends a voice message — `load()` is the only cost and
        # nothing calls it again.
        threading.Thread(target=self._warm_stt, daemon=True,
                         name="telegram-stt-warm").start()
        # Same idea for the voice going out: load Piper once, up front,
        # rather than paying its (small, but non-zero) load time on
        # whichever message happens to be first.
        threading.Thread(target=self._warm_tts, daemon=True,
                         name="telegram-tts-warm").start()

        # Downloads, transcoding and transcription — everything in `_prepare`
        # that touches the network or a CPU-bound model rather than the vault
        # — run here, off the queue entirely, so a slow one never sits in
        # front of a fast one. `getUpdates` itself is never submitted to this
        # pool: it must keep running even if every worker is busy.
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_PREP_WORKERS, thread_name_prefix="telegram-prep")

        offset = self._offset()
        while True:
            try:
                updates = _call(self.s, "getUpdates", offset=offset,
                               timeout=self.s.telegram_poll_timeout,
                               allowed_updates=["message", "callback_query"])
            except (TelegramError, httpx.HTTPError) as exc:
                print(f"telegram: getUpdates failed ({_scrub(exc)}) — "
                     f"retrying in 5s")
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self._save_offset(offset)
                pool.submit(self._prep_and_enqueue, upd)

    def _transcriber(self):
        from . import stt
        with self._stt_lock:
            if self._stt is None:
                self._stt = stt.Transcriber(self.s)
                self._stt.load()   # the one-time download/load happens here
            return self._stt

    def _warm_stt(self) -> None:
        try:
            self._transcriber()
        except Exception:
            pass  # the real attempt at transcribe-time reports this properly

    def _synth(self):
        from . import tts
        with self._tts_lock:
            if self._tts is None:
                # `tts.synthesizer` picks the provider and, on the hosted one,
                # wraps Piper underneath it. Printing on fallback rather than
                # swallowing it: a voice note that quietly arrives in the wrong
                # voice is the kind of degradation nobody notices for a month.
                self._tts = tts.synthesizer(
                    self.s,
                    on_fallback=lambda why: print(
                        f"telegram: hosted voice unavailable, using piper — {why}"))
                self._tts.load()   # Unavailable here if nothing can speak at all
            return self._tts

    def _warm_tts(self) -> None:
        if not _tts_enabled(self.s):
            return
        try:
            self._synth()
        except Exception:
            pass  # the real attempt at reply-time degrades to text-only

    # ---- the prep pool: everything slow, nothing that touches the engine --
    def _prep_and_enqueue(self, upd: dict) -> None:
        try:
            item = self._prepare(upd)
        except Exception as exc:
            print(f"telegram: prep for update {upd.get('update_id')} failed: "
                 f"{type(exc).__name__}: {_scrub(exc)}")
            return
        if item is not None:
            self._queue.put(item)

    def _prepare(self, upd: dict) -> dict | None:
        """Downloads, transcodes, transcribes — whatever an update needs
        before it's ready for `_commit` to touch the vault. Runs on a prep
        thread, never on the worker that owns the `Engine`, so a slow one
        never blocks anything behind it. Returns `None` for updates there is
        nothing to do with."""
        if "callback_query" in upd:
            return {"kind": "callback", "cq": upd["callback_query"]}

        msg = upd.get("message")
        if not msg:
            return None
        user_id = (msg.get("from") or {}).get("id")
        chat = msg["chat"]
        chat_type = chat.get("type", "private")

        if not self.s.telegram_owner_id and chat_type == "private":
            return {"kind": "claim", "user_id": user_id, "chat_id": chat["id"]}
        if not self._is_owner(user_id):
            return None  # silently — a stranger who finds the bot learns nothing

        if chat_type in ("group", "supergroup"):
            return self._prepare_archive(msg, chat)
        return self._prepare_converse(msg, chat["id"])

    def _prepare_converse(self, msg: dict, chat_id: int) -> dict:
        text = (msg.get("text") or msg.get("caption") or "").strip()

        first_word = text.lower().split(" ", 1)[0] if text else ""
        if first_word == "/new":
            return {"kind": "new_session", "chat_id": chat_id}
        if first_word in _IGNORED_COMMANDS:
            return {"kind": "start", "chat_id": chat_id}

        out = {"kind": "converse", "chat_id": chat_id, "text": text,
              "message_type": "text", "has_location": "location" in msg,
              "file_bytes": None, "file_name": None}

        if out["has_location"]:
            loc = msg["location"]
            out["location"] = f"{loc['latitude']},{loc['longitude']}"
            return out

        if "voice" in msg:
            data, _name = self._download(msg["voice"]["file_id"])
            transcript = self._transcribe(data)
            out["message_type"] = "voice"
            out["text"] = transcript or text
            out["transcribe_failed"] = not transcript
            return out

        file_id, name, kind = self._attachment(msg)
        if file_id:
            data, remote_name = self._download(file_id)
            out["file_bytes"] = data
            out["file_name"] = name or remote_name
            out["message_type"] = kind
        return out

    def _prepare_archive(self, msg: dict, chat: dict) -> dict:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        file_id, name, kind = self._attachment(msg)
        if "voice" in msg:
            file_id, name, kind = msg["voice"]["file_id"], None, "voice"
        is_audio = kind in ("audio", "voice")

        file_bytes = file_name = None
        if file_id:
            file_bytes, remote_name = self._download(file_id)
            file_name = name or remote_name

        return {"kind": "archive", "chat": chat, "msg": msg, "text": text,
               "is_audio": is_audio, "file_bytes": file_bytes,
               "file_name": file_name}

    # ---- files ----------------------------------------------------------
    def _download(self, file_id: str) -> tuple[bytes, str]:
        info = _call(self.s, "getFile", file_id=file_id)
        path = info["file_path"]
        url = FILE_URL.format(token=self.s.telegram_bot_token, path=path)
        r = httpx.get(url, timeout=60)
        r.raise_for_status()
        return r.content, Path(path).name

    def _save_attachment(self, engine: Engine, data: bytes,
                         filename: str | None) -> str:
        ext = Path(filename or "").suffix
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = _safe_filename(Path(filename or "file").stem) or "file"
        rel = f"attachments/telegram/{stamp}-{safe}{ext}"
        n = 2
        while engine.vault.exists(rel):
            rel = f"attachments/telegram/{stamp}-{safe}-{n}{ext}"
            n += 1
        engine.vault.save_binary(rel, data, summary="telegram attachment")
        return rel

    @staticmethod
    def _attachment(msg: dict) -> tuple[str | None, str | None, str]:
        """(file_id, a display name, the kind) — or (None, None, "text")."""
        if "photo" in msg:
            largest = msg["photo"][-1]
            return largest["file_id"], None, "photo"
        if "video" in msg:
            v = msg["video"]
            return v["file_id"], v.get("file_name"), "video"
        if "audio" in msg:
            a = msg["audio"]
            return a["file_id"], a.get("title") or a.get("file_name"), "audio"
        if "document" in msg:
            d = msg["document"]
            return d["file_id"], d.get("file_name"), "document"
        return None, None, "text"

    def _transcribe(self, data: bytes) -> str:
        try:
            from . import stt
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / "voice.ogg"
                src.write_bytes(data)
                wav = stt.convert_to_wav(src)
                transcriber = self._transcriber()   # blocks only if a load is in flight
                with self._stt_lock:                # serialise inference itself
                    return transcriber.transcribe(wav).text.strip()
        except Exception:
            return ""  # no ffmpeg / no faster-whisper — degrade, don't crash

    # ---- the serial worker: the one thread that owns the Engine -----------
    def _worker(self) -> None:
        # One `Engine`, one thread, for the same reason the scheduler needed
        # its own: a sqlite3 connection belongs to whichever thread opened it.
        #
        # `start_session()` here is what makes Telegram a conversation rather
        # than a string of one-off asks — without it every message opens its
        # own Hermes thread and "book him 15:30" right after "what's Arash's
        # calendar" has no idea who "him" is, same failure the REPL's own
        # session existed to fix. `atexit` mirrors the REPL's crash-safety:
        # a killed process still gets its recap written to the vault, because
        # losing the conversation is the one outcome that feature exists to
        # prevent, and `end_session` is idempotent — a second call after a
        # clean `/new` finds nothing open and writes nothing.
        import atexit

        engine = Engine(self.s, surface=persona_mod.TELEGRAM)
        engine.start_session()
        atexit.register(_safe_end_session, engine)
        try:
            while True:
                item = self._queue.get()
                try:
                    self._commit(item, engine)
                except Exception as exc:  # one bad item must not end the loop
                    print(f"telegram: commit failed: "
                         f"{type(exc).__name__}: {_scrub(exc)}")
        finally:
            _safe_end_session(engine)
            engine.close()

    def _commit(self, item: dict, engine: Engine) -> None:
        kind = item["kind"]
        if kind == "claim":
            self._claim(item["user_id"])
            self._reply(item["chat_id"],
                       "You're the only one I'll listen to from here on, sir.")
        elif kind == "start":
            self._reply(item["chat_id"],
                       "Online, sir. Just say what you want — /new to start "
                       "a fresh thread.")
        elif kind == "new_session":
            engine.end_session("new")
            engine.start_session()
            self._reply(item["chat_id"],
                       "Fresh thread, sir — nothing before this point "
                       "carries over.")
        elif kind == "callback":
            self._commit_callback(item["cq"], engine)
        elif kind == "converse":
            self._commit_converse(item, engine)
        elif kind == "archive":
            self._commit_archive(item, engine)

    # ---- the conversational path (private chat) --------------------------
    def _commit_converse(self, item: dict, engine: Engine) -> None:
        chat_id = item["chat_id"]
        if item.get("transcribe_failed"):
            self._reply(chat_id,
                       "Couldn't transcribe that (no speech engine "
                       "available), sir — text me instead?")
            return

        extra: dict = {}
        has_file = False
        if "location" in item:
            extra["location"] = item["location"]
        elif item.get("file_bytes") is not None:
            extra["file_rel"] = self._save_attachment(
                engine, item["file_bytes"], item["file_name"])
            has_file = True

        with self._chat_action(chat_id, "typing"):
            resp = engine.handle(item["text"], message_type=item["message_type"],
                                 has_location=item["has_location"], has_file=has_file,
                                 extra=extra)
        self._reply(chat_id, _response_text(resp), buttons=resp.buttons)

    def _commit_callback(self, cq: dict, engine: Engine) -> None:
        user_id = (cq.get("from") or {}).get("id")
        if not self._is_owner(user_id):
            try:
                _call(self.s, "answerCallbackQuery", callback_query_id=cq["id"])
            except (TelegramError, httpx.HTTPError):
                pass
            return
        entry = _CALLBACKS.pop(cq.get("data", ""), None)
        chat_id = cq["message"]["chat"]["id"]
        try:
            if not entry:
                _call(self.s, "answerCallbackQuery", callback_query_id=cq["id"],
                     text="That button expired.")
                return
            _call(self.s, "answerCallbackQuery", callback_query_id=cq["id"])
        except (TelegramError, httpx.HTTPError):
            pass
        if not entry:
            return
        action, payload = entry
        with self._chat_action(chat_id, "typing"):
            resp = engine.act(action, payload)
        self._reply(chat_id, _response_text(resp), buttons=resp.buttons)

    # ---- the archive path (any group the owner controls) ------------------
    def _commit_archive(self, item: dict, engine: Engine) -> None:
        chat, msg = item["chat"], item["msg"]
        title = chat.get("title") or f"chat-{chat['id']}"
        slug = _safe_filename(title) or f"chat-{chat['id']}"
        text = item["text"]

        file_rel = None
        if item.get("file_bytes") is not None:
            file_rel = self._save_attachment(engine, item["file_bytes"],
                                             item["file_name"])

        if not text and not file_rel:
            return  # e.g. a "member joined" service message — nothing to file

        folder = self.s.folders.music if item["is_audio"] else self.s.folders.braindump
        rel = f"{folder}/Telegram Archive/{slug}.md"
        when = dt.datetime.now()
        line = f"- {when.strftime('%Y-%m-%d %H:%M')}" + (f" — {text}" if text else "")
        if file_rel:
            line += f"\n  ![[{file_rel}]]"

        if not engine.vault.exists(rel):
            meta = {"type": "telegram-archive", "source-chat": title,
                    "chat-id": chat["id"], "created": when.date().isoformat(),
                    "tags": ["telegram-archive"]}
            engine.vault.create(rel, meta, f"\n{line}\n",
                                summary=f"telegram archive: {title}")
        else:
            engine.vault.append(rel, line, summary=f"telegram archive: {title}")

        # A receipt, not a reply — the group has no one to read a reply, and a
        # message back into it would be V talking to itself. Best-effort:
        # `setMessageReaction` isn't in every Bot API version's vocabulary.
        try:
            _call(self.s, "setMessageReaction", chat_id=chat["id"],
                 message_id=msg["message_id"],
                 reaction=[{"type": "emoji", "emoji": "👍"}])
        except (TelegramError, httpx.HTTPError):
            pass


# ---- entry point ----------------------------------------------------------
def _announce_offline(s: Settings, *_args) -> None:
    """The other half of the online notice below — a reply that used to
    just stop coming, with no way to tell "V is restarting" apart from
    "something's actually wrong", now says so on the way out. Registered
    for both SIGTERM (how a redeploy/restart normally stops this process —
    `pkill -TERM`, systemd, etc.) and SIGINT (Ctrl-C), so either path gets
    the notice; raising `SystemExit` afterward hands control back to
    Python's normal shutdown instead of the OS's default "just die",
    which is what let a signal arriving mid-`getUpdates` interrupt the
    blocking call promptly rather than waiting out its 30s timeout."""
    if s.telegram_owner_id:
        send_message(s, s.telegram_owner_id, "Going offline, sir — back shortly.")
    raise SystemExit(0)


def run(settings: Settings | None = None) -> None:
    """Poll for messages, and drive the proactive loop on its own thread.

    Engines never cross a thread boundary: the update-fetch loop and the prep
    pool never touch one, `TelegramBot._worker` owns exactly one, and the
    scheduler builds its own. A sqlite3 connection may only be used on the
    thread that opened it — the same rule `mcp/server.py` gives each worker
    thread its own `_Engines()` bundle for.
    """
    s = settings or default_settings
    if not s.telegram_bot_token:
        raise TelegramError(
            "telegram_bot_token is not set — export TELEGRAM_BOT_TOKEN or "
            "add it to " + str(self.s.state_dir / "config.json"))
    from .scheduler import Scheduler

    def _run_scheduler() -> None:
        sched_engine = Engine(s, surface=persona_mod.SCHEDULER)
        try:
            Scheduler(sched_engine, s, sink=build_sink(s)).run(interval=30)
        finally:
            sched_engine.close()

    threading.Thread(target=_run_scheduler, daemon=True,
                     name="telegram-scheduler").start()

    # Signal handlers only make sense on the main thread — harmless no-op
    # if `run()` is ever driven from a non-main thread (tests do this).
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: _announce_offline(s))
        signal.signal(signal.SIGINT, lambda signum, frame: _announce_offline(s))
    except ValueError:
        pass

    if s.telegram_owner_id:
        send_message(s, s.telegram_owner_id, "Online, sir — ready when you are.")

    TelegramBot(s).run()
