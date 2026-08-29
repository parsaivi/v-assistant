"""Speech to text, locally, with faster-whisper. $0 and nothing leaves the box.

The privacy argument outweighs the money here: voice memos are the most personal
input the assistant will ever take, and hosted transcription would ship all of
them off the machine for about $3.60/month of "savings" in effort.

The open question from the design doc is Persian speed — `large-v3` is accurate
but slow on CPU. `benchmark()` answers it with a measurement instead of a guess.
"""

from __future__ import annotations

import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, settings as default_settings


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    text: str
    language: str
    language_probability: float
    duration: float
    seconds_taken: float
    model: str
    segments: list[Segment] = field(default_factory=list)
    cost: float = 0.0

    @property
    def realtime_factor(self) -> float:
        """Seconds of compute per second of audio. Below 1.0 is faster than real time."""
        return self.seconds_taken / self.duration if self.duration else 0.0

    def summary(self) -> str:
        return (f"{self.language} ({self.language_probability:.0%}) · "
                f"{self.duration:.0f}s audio in {self.seconds_taken:.1f}s "
                f"({self.realtime_factor:.2f}× realtime) · {self.model}")


class Unavailable(RuntimeError):
    pass


class Transcriber:
    """Lazily loads the model — importing this module must stay cheap."""

    #: What every path here resamples to, and what the phone already records.
    RATE = 16_000

    def __init__(self, settings: Settings | None = None, model_size: str | None = None):
        self.s = settings or default_settings
        self.model_size = model_size or self.s.whisper_model
        self._model = None

    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise Unavailable(
                "faster-whisper is not installed — `pip install faster-whisper`"
            ) from exc
        cache = self.s.state_dir.parent / "models"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            self._model = WhisperModel(
                self.model_size, device="cpu",
                compute_type=self.s.whisper_compute_type,
                download_root=str(cache),
            )
        except Exception:
            # Fall back to a smaller model rather than failing the capture.
            self._model = WhisperModel(
                self.s.whisper_fallback_model, device="cpu",
                compute_type=self.s.whisper_compute_type,
                download_root=str(cache),
            )
            self.model_size = self.s.whisper_fallback_model
        return self._model

    def transcribe(self, audio: Path | str, *, language: str | None = None,
                   vad: bool = True) -> Transcript:
        audio = Path(audio)
        if not audio.exists():
            raise FileNotFoundError(audio)
        model = self.load()
        t0 = time.time()
        segments, info = model.transcribe(
            str(audio),
            language=language or self.s.whisper_language,
            vad_filter=vad,
            beam_size=5,
            condition_on_previous_text=False,  # reduces looping on long memos
        )
        segs = [Segment(s.start, s.end, s.text.strip()) for s in segments]
        elapsed = time.time() - t0
        return Transcript(
            text=" ".join(s.text for s in segs).strip(),
            language=info.language,
            language_probability=float(info.language_probability),
            duration=float(info.duration),
            seconds_taken=elapsed,
            model=self.model_size,
            segments=segs,
        )

    def transcribe_pcm(self, pcm: bytes, *, language: str | None = None,
                       initial_prompt: str | None = None,
                       vad: bool = True) -> Transcript:
        """Transcribe raw PCM16LE at [RATE] with no file and no ffmpeg.

        The streaming path calls this several times per utterance, so the temp
        file and the subprocess that `transcribe` pays once are worth removing.
        """
        model = self.load()
        audio = pcm_to_float(pcm)
        duration = len(audio) / self.RATE
        t0 = time.time()
        segments, info = model.transcribe(
            audio,
            language=language or self.s.whisper_language,
            vad_filter=vad,
            beam_size=5,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
        )
        segs = [Segment(s.start, s.end, s.text.strip()) for s in segments]
        elapsed = time.time() - t0
        return Transcript(
            text=" ".join(s.text for s in segs).strip(),
            language=info.language,
            language_probability=float(info.language_probability),
            # `info.duration` is what the VAD kept, which is not what was sent
            # and would make the realtime factor flatter than the truth.
            duration=duration,
            seconds_taken=elapsed,
            model=self.model_size,
            segments=segs,
        )

    # ---- the measurement the design doc asked for --------------------
    def benchmark(self, audio: Path | str | None = None,
                  sizes: tuple[str, ...] = ("tiny", "base", "small", "medium",
                                            "large-v3")) -> list[dict]:
        """Time each model size on a sample. Answers 'is Persian voice viable?'"""
        audio = Path(audio) if audio else _synth_sample(self.s)
        out = []
        for size in sizes:
            try:
                t = Transcriber(self.s, model_size=size).transcribe(audio)
                out.append({
                    "model": size, "seconds": round(t.seconds_taken, 2),
                    "audio_seconds": round(t.duration, 1),
                    "realtime_factor": round(t.realtime_factor, 2),
                    "language": t.language,
                    "text": t.text[:120],
                })
            except Exception as exc:
                out.append({"model": size, "error": str(exc)[:120]})
        return out


def convert_to_wav(src: Path, dst: Path | None = None) -> Path:
    """Telegram sends OGG/Opus; whisper is happiest with 16kHz mono WAV.

    The derived destination has to dodge the source. A caller handing this a
    file that is *already* `.wav` — which the phone transport does on every
    voice message, since `AudioRecord` gives it raw PCM — would otherwise get
    `dst == src`, and ffmpeg refuses to write its own input: "Output ... same
    as Input #0 - exiting". The sample rate and channel count still need
    normalising in that case, so the answer is a different name rather than
    skipping the conversion.
    """
    dst = dst or src.with_suffix(".wav")
    if dst == src:
        dst = src.with_name(f"{src.stem}-16k.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", str(dst)],
        check=True, capture_output=True,
    )
    return dst


def _synth_sample(s: Settings) -> Path:
    """A 5-second silent WAV, so benchmark() runs with no sample on hand."""
    path = s.state_dir / "bench_silence.wav"
    if not path.exists():
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 5)
    return path


# ---- transcribing while he is still talking --------------------------------
# The measurement that forced this: `large-v3` on Parsa's i7-1355U runs at
# ~8x realtime, so 3.2 seconds of speech cost 25.5 seconds to transcribe — and
# none of it started until the button was released and the whole WAV had been
# uploaded. The model choice is one fix (`small` measures 1.09x on the same
# clip); overlapping the work with the speaking is the other, and they compose.
#
# The phone already runs Silero VAD to find utterance boundaries for the wake
# gate, so it can cut the recording at a silence and post each piece as it
# closes. By the time the finger lifts, everything but the last piece has
# already been through the model.
#
# Chunks are cut at silence *by the sender*, which is what makes this sound
# rather than clever: whisper is being handed complete phrases, not a stream
# sliced on a timer mid-word. `initial_prompt` carries the text so far, so the
# decoder still has the sentence's history to condition on across a boundary.

#: Below this a chunk is held rather than run, and this number is the whole
#: design. Whisper pads *every* call to a 30-second mel window, so the encoder
#: costs the same for 0.5s of audio as for 25s and N chunks means N encoder
#: passes. Measured: cutting a 3.2s clip into four 1s pieces took 8.1s of
#: compute against 1.6s for the same clip in one pass — five times the work,
#: to overlap three seconds.
#:
#: So the rule is not "stream everything". It is: an utterance short enough to
#: be one pass stays one pass and costs exactly what it costs today, and only
#: a long dictation — where there is real speaking time to hide the work
#: behind — gets cut. At 6 seconds a 25-second memo becomes four passes that
#: finish as he talks, and a 4-second question stays one pass on release.
MIN_CHUNK_SECONDS = 6.0


def pcm_to_float(pcm: bytes):
    """PCM16LE bytes → the float32 array faster-whisper wants.

    Handing the model an array rather than a path is not a micro-optimisation
    here: it removes the temp file *and* the ffmpeg subprocess from every
    chunk, and this path runs several times per utterance instead of once.
    """
    import numpy as np
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0


class Stream:
    """One utterance, transcribed in pieces as they arrive.

    Not thread-safe by construction and does not need to be: a single utterance
    has one sender, and `httpapi` serialises whisper behind the same lock the
    one-shot path uses.
    """

    def __init__(self, transcriber: "Transcriber", language: str | None = None):
        self.t = transcriber
        self.language = language
        self.parts: list[str] = []
        self.seconds = 0.0
        self.compute_seconds = 0.0
        self._held = b""          # too short to run on its own, yet

    @property
    def text(self) -> str:
        return " ".join(p for p in self.parts if p).strip()

    def push(self, pcm: bytes, *, final: bool = False) -> str:
        """Add audio; returns the transcript so far.

        A chunk shorter than [MIN_CHUNK_SECONDS] is held rather than run,
        unless this is the last one — a held tail must never be silently
        dropped, which is the bug this is written to not have. That also means
        a short utterance runs exactly once, on `final`, which is the point:
        this path must never be slower than not having it.
        """
        self._held += pcm
        held_seconds = len(self._held) / 2 / Transcriber.RATE
        if not final and held_seconds < MIN_CHUNK_SECONDS:
            return self.text
        if not self._held:
            return self.text
        audio, self._held = self._held, b""
        self.seconds += len(audio) / 2 / Transcriber.RATE
        # No `initial_prompt`. Feeding the text so far back in as a prompt is
        # the obvious way to keep context across a boundary and it is a trap:
        # measured, the decoder re-emitted the prompt as transcript, turning
        # "Good evening sir. Report 4 is done." into "Report. What four is
        # done. Good evening, sir." A chunk cut at a silence is a complete
        # phrase and needs no help; a wrong word is cheaper than a duplicated
        # sentence.
        said = self.t.transcribe_pcm(audio, language=self.language)
        self.compute_seconds += said.seconds_taken
        if said.text.strip():
            self.parts.append(said.text.strip())
        return self.text

    def summary(self) -> str:
        rt = self.compute_seconds / self.seconds if self.seconds else 0.0
        return (f"{self.seconds:.1f}s audio in {self.compute_seconds:.1f}s "
                f"({rt:.2f}x realtime) · {len(self.parts)} chunks · "
                f"{self.t.model_size}")
