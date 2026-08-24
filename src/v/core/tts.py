"""Text to speech. A hosted voice with a local one underneath it.

Two synthesizers with one interface (`available` / `load()` / `synthesize()`),
and `synthesizer()` at the bottom picks between them:

- `RemoteSynthesizer` — OpenRouter's speech endpoint. This is the voice V is
  supposed to have: the persona's own, British and level, ~3s for a short
  reply, billed per character.
- `MixedSynthesizer` — Piper, locally. $0, offline, always there, and audibly
  a text-to-speech engine.

The second is not a rival to the first, it is what happens when the first
fails. `Fallback` wires them together so a dead network, an expired key or an
empty balance costs the accent rather than the answer — the same instinct as
`engine._escalate` degrading to local search rather than apologising.

Mirrors `stt.py`'s shape on purpose — lazily-loaded model, `Unavailable`
when the package or the voice files aren't there, degrade instead of crash.

Deliberately transport-agnostic: `Synthesizer.synthesize()` returns a WAV
file as bytes, full stop. Telegram is the first interface wired up to it
(see `core/telegram.py`), but nothing in this module knows Telegram
exists — a future WhatsApp/Signal/CLI-with-speakers interface reuses this
same class and just picks its own output format.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

from . import persona
from .config import Settings, settings as default_settings

ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"

# Persian/Arabic script blocks — covers Persian letters, digits, and
# punctuation (، ؟) that Piper's English voice has no phonemes for.
_PERSIAN_CHAR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def _segments(text: str) -> list[tuple[str, bool]]:
    """Split `text` into runs of `(chunk, is_persian)`. Whitespace is folded
    into whichever side it borders rather than starting a run of its own —
    otherwise every space inside a sentence would flip the voice."""
    if not text:
        return []
    classes: list[str | None] = []
    last = None
    for ch in text:
        if ch.isspace():
            classes.append(last)
        else:
            c = "fa" if _PERSIAN_CHAR.match(ch) else "en"
            classes.append(c)
            last = c
    first_real = next((c for c in classes if c is not None), "en")
    classes = [c or first_real for c in classes]
    out: list[tuple[str, bool]] = []
    start = 0
    for i in range(1, len(classes) + 1):
        if i == len(classes) or classes[i] != classes[start]:
            out.append((text[start:i], classes[start] == "fa"))
            start = i
    return out


class Unavailable(RuntimeError):
    pass


class Synthesizer:
    """Lazily loads the model — importing this module must stay cheap."""

    def __init__(self, settings: Settings | None = None, voice: str | None = None):
        self.s = settings or default_settings
        # The config field is an override, empty by default; the voice really
        # belongs to the character (`core/persona.py`), so that switching
        # persona switches how V sounds on this path too and not only on the
        # hosted one.
        self.voice = voice or self.s.tts_voice or persona.active(self.s).spoken.piper
        self._model = None

    def _model_dir(self) -> Path:
        return self.s.model_dir / "piper"

    def _model_path(self) -> Path:
        return self._model_dir() / f"{self.voice}.onnx"

    @property
    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return self._model_path().exists()

    def load(self):
        if self._model is not None:
            return self._model
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise Unavailable(
                "piper-tts is not installed — `pip install piper-tts`"
            ) from exc
        path = self._model_path()
        if not path.exists():
            raise Unavailable(
                f"voice model missing: {path} — download the .onnx and "
                f".onnx.json for '{self.voice}' from "
                f"https://huggingface.co/rhasspy/piper-voices into {path.parent}"
            )
        self._model = PiperVoice.load(str(path))
        return self._model

    def synthesize(self, text: str) -> bytes:
        """Text in, a WAV file out, in memory. Piper is fast enough on CPU
        (well under real time for a sentence) that there's no reason to
        make this async at this layer — callers that care run it off their
        own thread."""
        model = self.load()
        from piper.config import SynthesisConfig
        speed = self.s.tts_speed or 1.0
        syn_config = SynthesisConfig(length_scale=1.0 / speed)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            model.synthesize_wav(text, wav_file, syn_config=syn_config)
        return buf.getvalue()


class MixedSynthesizer:
    """`Synthesizer` is one voice, one language — fine for `stt.py`'s
    mirror-shape, but a reply built for an English-speaking Parsa still
    quotes Persian note titles and words verbatim. This splits the text by
    script and hands each run to whichever voice actually has phonemes for
    it, so Persian doesn't come out as espeak spelling unknown letters one
    at a time. Both voices share the config's sample rate (22050 Hz for
    every Piper "medium" model), so the runs' raw PCM frames concatenate
    directly with no resampling step."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        spoken = persona.active(self.s).spoken
        self.en = Synthesizer(self.s, voice=self.s.tts_voice or spoken.piper)
        self.fa = Synthesizer(self.s, voice=self.s.tts_voice_fa or spoken.piper_fa)

    @property
    def available(self) -> bool:
        return self.en.available

    def load(self):
        self.en.load()
        if self.fa.available:
            try:
                self.fa.load()
            except Unavailable:
                pass  # Persian runs just fall back to the English voice
        return self

    def synthesize(self, text: str) -> bytes:
        frames = []
        params = None
        for chunk, is_fa in _segments(text):
            if not chunk.strip():
                continue
            synth = self.fa if (is_fa and self.fa.available) else self.en
            with wave.open(io.BytesIO(synth.synthesize(chunk)), "rb") as wf:
                if params is None:
                    params = wf.getparams()
                frames.append(wf.readframes(wf.getnframes()))
        if not frames:
            return b""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams(params)
            for f in frames:
                out.writeframes(f)
        return buf.getvalue()


# Fixed answers are most of what V says out loud. `/new` returns one constant
# sentence, `engine._smalltalk` has five, `/status` and the nudges are
# templates over small numbers — and `telegram._reply` speaks every one of
# them unconditionally. Without a cache each of those is a paid round-trip
# for audio that is identical to the last time, which is both the bill and
# the two seconds of "recording voice…" before a sentence Parsa has heard
# fifty times.
#
# Content-addressed rather than a list of known-fixed strings: "Good evening,
# sir. 2 reminders pending." caches on its own and misses the day the count
# changes, which is exactly right and needs nobody to maintain a registry of
# what counts as fixed.
CACHE_MAX_CHARS = 400        # a real answer is unique; only boilerplate repeats
CACHE_MAX_FILES = 400        # ~15MB of opus-ish mp3, pruned oldest-first


class VoiceCache:
    """Synthesized audio on disk, keyed by everything that shapes the sound.

    Under `state_dir` rather than `model_dir` for the same reason the search
    index is: rebuilt from scratch on demand, cheap to lose, and not a
    downloaded artefact anyone would think to back up.
    """

    def __init__(self, settings: Settings):
        self.dir = settings.state_dir / "voice"

    def key(self, ident: str, text: str) -> str:
        return hashlib.sha256(f"{ident}\0{text}".encode()).hexdigest()[:24]

    def get(self, ident: str, text: str) -> bytes | None:
        path = self.dir / f"{self.key(ident, text)}.audio"
        try:
            data = path.read_bytes()
        except OSError:
            return None
        # Touch on read so pruning is least-recently-*used*, not oldest-written:
        # the five smalltalk lines should outlive a one-off that happened to be
        # short, however long ago either was first said.
        try:
            path.touch()
        except OSError:
            pass
        return data or None

    def put(self, ident: str, text: str, audio: bytes) -> None:
        """Never let a cache write cost a reply that already succeeded — the
        audio is in hand by this point, so a full disk is a shrug, not an
        error."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".{self.key(ident, text)}.part"
            tmp.write_bytes(audio)
            tmp.replace(self.dir / f"{self.key(ident, text)}.audio")
            self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.audio"), key=lambda f: f.stat().st_mtime)
        for stale in files[:-CACHE_MAX_FILES]:
            try:
                stale.unlink()
            except OSError:
                pass


class RemoteSynthesizer:
    """The persona's actual voice, from OpenRouter's speech endpoint.

    One POST, text in and an MP3 out — the endpoint is OpenAI-Audio-shaped, so
    `model` / `input` / `voice` / `response_format` mean what they mean
    everywhere else. No streaming: a voice message is sent as a file, so there
    is nothing to do with a first chunk that arrives early, and a single
    request is one thing to time out rather than two.

    MP3 rather than the endpoint's default PCM because PCM arrives with no
    header — sample rate and channel count would have to be known per model
    and rebuilt into a WAV by hand, and getting that wrong is silent
    chipmunk-speed audio rather than an error. MP3 carries its own rate and
    `to_ogg_opus` re-encodes it either way.

    Unlike `Synthesizer` there is no model to load, so `load()` only checks
    the key. It exists to keep the interface identical: `telegram.py` and
    `httpapi.py` both warm their synthesizer at startup and expect
    `Unavailable` there rather than on the first reply.
    """

    def __init__(self, settings: Settings | None = None,
                 model: str | None = None, voice: str | None = None):
        self.s = settings or default_settings
        spoken = persona.active(self.s).spoken
        self.model = model or self.s.tts_model or spoken.model
        self.voice = voice or self.s.tts_remote_voice or spoken.voice
        self.reference = spoken.reference
        self._encoded = None            # the clip, base64'd once per process
        self.cache = VoiceCache(self.s)

    def reference_path(self) -> Path | None:
        """Where this persona's voice clip lives, or None for a preset voice.

        Under `model_dir` rather than `state_dir` because it is the same kind
        of thing as the Piper voices and the Whisper weights: a downloaded
        artefact, replaceable, and emphatically not something to sync with the
        notes.
        """
        if not self.reference:
            return None
        return self.s.model_dir / "voices" / self.reference

    def _clip(self) -> str:
        """Base64 of the reference clip, read once and kept.

        Encoding a 300KB file per reply is not expensive, but it is pure waste
        in a daemon that answers all day, and the file cannot change under a
        running process in any way worth reacting to.
        """
        if self._encoded is None:
            path = self.reference_path()
            self._encoded = base64.b64encode(path.read_bytes()).decode()
        return self._encoded

    def ident(self) -> str:
        """Everything that changes how a given sentence sounds.

        The reference clip is fingerprinted by size and mtime rather than
        name: swapping `evie.mp3` for a better take keeps the filename and
        must still invalidate every line she has already said, or the new
        voice arrives only for sentences nobody has heard yet — which is the
        kind of bug that looks like the clone "sometimes" not working.
        """
        parts = [self.model, self.voice, str(self.s.tts_speed or 1.0)]
        path = self.reference_path()
        if path is not None:
            try:
                st = path.stat()
                parts += [self.reference, str(st.st_size), str(int(st.st_mtime))]
            except OSError:
                parts.append(self.reference)
        return "|".join(parts)

    @property
    def available(self) -> bool:
        if not self.s.openrouter_api_key:
            return False
        path = self.reference_path()
        return path is None or path.exists()

    def load(self):
        if not self.s.openrouter_api_key:
            raise Unavailable(
                "no OpenRouter key — set OPENROUTER_API_KEY, or put "
                "`openrouter_api_key` in the state config, or set "
                "`tts_provider` to \"piper\" to use the local voice")
        path = self.reference_path()
        if path is not None and not path.exists():
            # The one dependency of a persona that is a file. Say the path:
            # the fix is copying a clip into place, and a message that does
            # not name the destination makes that a guessing game.
            raise Unavailable(
                f"{persona.active(self.s).key} speaks in a cloned voice and "
                f"its reference clip is missing: {path}")
        return self

    def synthesize(self, text: str) -> bytes:
        """Text in, an MP3 file out, in memory.

        Raises `Unavailable` for every failure mode — no key, a 4xx, a
        timeout, a truncated body — because the caller's question is only ever
        "did this produce speech", and `Fallback` below answers it by reaching
        for Piper. The message says which, since that is the difference
        between a wrong voice id (fix the persona) and a 402 (top up).
        """
        self.load()
        ident = self.ident()
        if len(text) <= CACHE_MAX_CHARS:
            hit = self.cache.get(ident, text)
            if hit is not None:
                return hit
        payload = {
            "model": self.model,
            "input": text,
            "response_format": "mp3",
            "speed": self.s.tts_speed or 1.0,
        }
        if self.reference:
            # Stateless cloning: the clip rides along with every request and
            # nothing is saved provider-side, so there is no voice to create,
            # expire, or clean up — the persona owns the file and that is the
            # whole state. `voice` is omitted rather than sent empty; Fish
            # reads an empty string as "use the default voice" and would
            # quietly ignore the clip.
            payload["input_references"] = [
                {"type": "input_audio",
                 "input_audio": {"data": self._clip(), "format": "mp3"}}]
        else:
            payload["voice"] = self.voice
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self.s.openrouter_api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.s.tts_timeout) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as exc:
            # The endpoint returns audio on success and JSON on failure, so an
            # error body is readable and worth reading — "An explicit voice is
            # required for this TTS provider" is a one-line fix, and a bare
            # "HTTP 400" is a debugging session.
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")
            except Exception:
                pass
            raise Unavailable(
                f"{self.model} refused the request (HTTP {exc.code})"
                + (f": {detail}" if detail else "")) from exc
        except Exception as exc:                 # timeout, DNS, reset socket
            raise Unavailable(
                f"could not reach OpenRouter: {type(exc).__name__}") from exc
        if not audio:
            raise Unavailable(f"{self.model} returned no audio")
        # Only a successful remote generation is ever stored. Piper's audio
        # never reaches this line, so an outage cannot leave the fallback
        # voice cached and replayed for weeks after the network came back.
        if len(text) <= CACHE_MAX_CHARS:
            self.cache.put(ident, text, audio)
        return audio


class Fallback:
    """A voice, and the voice that speaks when it can't.

    The whole point of this class is the `except` in `synthesize`. Everything
    else in this module raises `Unavailable` and lets the caller degrade to
    text; here the degrade is another synthesizer, so Parsa gets a reply read
    aloud in a worse voice instead of no reply read aloud at all.

    `load()` deliberately does not fail when the primary can't load. A missing
    key at startup is exactly the case this class exists for, and refusing to
    warm up would turn a recoverable one into a dead voice path.
    """

    def __init__(self, primary, backup, *, on_fallback=None):
        self.primary = primary
        self.backup = backup
        self.on_fallback = on_fallback
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self.primary.available or self.backup.available)

    def load(self):
        for s in (self.primary, self.backup):
            try:
                s.load()
            except Unavailable:
                pass
        if not self.available:
            raise Unavailable("neither the hosted voice nor Piper is available")
        return self

    def synthesize(self, text: str) -> bytes:
        try:
            return self.primary.synthesize(text)
        except Unavailable as exc:
            self.last_error = str(exc)
        except Exception as exc:                 # a bug in the remote path is
            self.last_error = f"{type(exc).__name__}: {exc}"   # not a mute V
        if self.on_fallback:
            self.on_fallback(self.last_error)
        return self.backup.synthesize(text)


def synthesizer(settings: Settings | None = None, *, on_fallback=None):
    """The one call every surface should make to get a voice.

    `telegram.py` and `httpapi.py` used to construct `MixedSynthesizer`
    themselves, which meant the choice of provider lived in two places and a
    third surface would have made it three. They ask here now, and this reads
    `tts_provider`:

    - "openrouter" — the persona's hosted voice, backed by Piper.
    - "piper"      — local only, the old behaviour exactly.
    - anything else ("null") — `Unavailable`, which every caller already
      handles by sending text and no voice note.
    """
    s = settings or default_settings
    if s.tts_provider == "openrouter":
        return Fallback(RemoteSynthesizer(s), MixedSynthesizer(s),
                        on_fallback=on_fallback)
    if s.tts_provider == "piper":
        return MixedSynthesizer(s)

    class _Off:
        available = False

        def load(self):
            raise Unavailable(f"tts_provider is {s.tts_provider!r} — nothing "
                              "speaks. Set it to \"openrouter\" or \"piper\".")

        def synthesize(self, text: str) -> bytes:
            return self.load()

    return _Off()


def to_ogg_opus(wav_bytes: bytes) -> bytes:
    """WAV (or MP3) → OGG/Opus — the format a voice-message bubble needs
    (Telegram's `sendVoice`, and WhatsApp's equivalent, both want this rather
    than a generic audio attachment). Kept here rather than in `telegram.py`
    because the format requirement isn't Telegram-specific, just
    voice-message-specific.

    The parameter is named `wav_bytes` for the local path and the temp file is
    still called `in.wav`, but MP3 from `RemoteSynthesizer` goes through this
    function untouched and comes out correct: ffmpeg identifies its input by
    content, never by the extension it was handed. That is why the remote path
    needs no decode step of its own."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / "out.ogg"
        src.write_bytes(wav_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-c:a", "libopus", "-b:a", "32k", "-vbr", "on", str(dst)],
            check=True, capture_output=True,
        )
        return dst.read_bytes()
