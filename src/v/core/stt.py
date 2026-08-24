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
