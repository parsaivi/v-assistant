#!/usr/bin/env python3
"""Rebuild the gate's model assets and the test's golden vectors.

The five files under `android/app/src/main/assets/models/` are binaries checked
into a repository that otherwise contains none, which is a thing worth being
able to undo. This script is how they were made, so none of them is magic:
running it reproduces every one, and the golden vectors that `FeaturesTest`
checks the Kotlin against come from the same run as the filterbank the app
ships — which is the property that makes that test mean anything.

    ./.venv/bin/python docs/ambient-wake/prepare_models.py

Needs `librosa`, `onnx` and `onnxruntime`, none of which are runtime
dependencies of V. They are build-time tools for this one script and are
deliberately not in `pyproject.toml`:

    ./.venv/bin/pip install librosa onnx onnxruntime

`--verify` additionally re-runs the speaker-separation measurement quoted in
README §3 against two voices, which is the check that says the whole chain is
still wired correctly rather than merely still running.
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "android/app/src/main/assets/models"
TEST_RES = ROOT / "android/app/src/test/resources"

OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
SILERO = ("https://github.com/snakers4/silero-vad/raw/master/"
          "src/silero_vad/data/silero_vad.onnx")
SPEAKER = ("https://huggingface.co/csukuangfj/speaker-embedding-models/"
           "resolve/main/nemo_en_titanet_small.onnx")

# NeMo's featurizer, which is what TitaNet was trained on. These are mirrored
# in Features.kt; changing one without the other is the failure the golden
# vectors exist to catch.
SR, N_FFT, HOP, WIN, N_MELS = 16_000, 512, 160, 400, 80


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  · {dest.name} already here")
        return
    print(f"  ↓ {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def nemo_feats(x, mel):
    """The reference implementation. `Features.logMel` is a port of this."""
    import numpy as np
    import librosa
    y = np.append(x[0], x[1:] - 0.97 * x[:-1])
    s = librosa.stft(y, n_fft=N_FFT, hop_length=HOP, win_length=WIN,
                     window="hann", center=True, pad_mode="reflect")
    m = np.log((mel @ (np.abs(s) ** 2)) + 2 ** -24)
    return (m - m.mean(1, keepdims=True)) / (m.std(1, keepdims=True) + 1e-5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="re-measure speaker separation on two voices")
    args = ap.parse_args()

    import numpy as np
    import librosa
    import onnx

    ASSETS.mkdir(parents=True, exist_ok=True)
    TEST_RES.mkdir(parents=True, exist_ok=True)

    print("models:")
    fetch(f"{OWW}/melspectrogram.onnx", ASSETS / "mel.onnx")
    fetch(f"{OWW}/embedding_model.onnx", ASSETS / "emb.onnx")
    fetch(SILERO, ASSETS / "vad.onnx")

    speaker = ASSETS / "speaker.onnx"
    if not speaker.exists():
        raw = ASSETS / "titanet_small_full.onnx"
        fetch(SPEAKER, raw)
        # Strip the 16,681-class classifier: the gate wants `embs` and never
        # `logits`, and the head is 13MB of an APK that carries it forever.
        print("  ✂ stripping the classifier head")
        onnx.utils.extract_model(str(raw), str(speaker),
                                 ["audio_signal", "length"], ["embs"])
        raw.unlink()
    print(f"  · speaker.onnx {speaker.stat().st_size / 1e6:.1f} MB")

    # The filterbank the app ships, so Kotlin never reimplements slaney mel.
    print("features:")
    mel = librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=N_MELS,
                              fmin=0, fmax=SR // 2, norm="slaney",
                              htk=False).astype("<f4")
    (ASSETS / "mel80.f32").write_bytes(mel.tobytes())
    (TEST_RES / "mel80.f32").write_bytes(mel.tobytes())
    print(f"  · mel80.f32 {mel.shape}")

    # Golden vectors. The signal is generated from three sinusoids rather than
    # recorded so FeaturesTest can build the identical input in Kotlin with no
    # audio file to drift.
    t = np.arange(SR)
    sig = (0.5 * np.sin(2 * np.pi * 220 * t / SR)
           + 0.3 * np.sin(2 * np.pi * 1300 * t / SR + 0.7)
           + 0.1 * np.sin(2 * np.pi * 3000 * t / SR)).astype(np.float32)
    g = nemo_feats(sig, mel).astype("<f4")
    (TEST_RES / "golden_logmel.f32").write_bytes(
        struct.pack("<ii", *g.shape) + g.tobytes())
    print(f"  · golden_logmel.f32 {g.shape}")

    if args.verify:
        if not verify(mel):
            return 1
    print("\ndone. `gradle :app:testDebugUnitTest` checks the Kotlin against these.")
    return 0


def verify(mel) -> bool:
    """Re-measure the number README §3 rests on.

    Same-speaker similarity must sit clear of different-speaker similarity, or
    no threshold exists that admits Parsa and refuses everyone else — which is
    exactly how the CAM++ candidate failed.
    """
    import numpy as np
    import onnxruntime as ort

    voices = Path.home() / ".cache/v/models/voices"
    pair = [voices / "evie.mp3", voices / "julian.mp3"]
    if not all(p.exists() for p in pair):
        print(f"\nverify: no voice samples in {voices}, skipping")
        return True

    print("\nverify: speaker separation")
    sess = ort.InferenceSession(str(ASSETS / "speaker.onnx"))
    segs = {}
    for p in pair:
        wav = p.with_suffix(".verify.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p),
                        "-ac", "1", "-ar", str(SR), "-f", "wav", str(wav)],
                       check=True)
        with wave.open(str(wav)) as w:
            a = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(np.float32) / 32768.0
        wav.unlink()
        out = []
        for i in range(6):
            f = nemo_feats(a[i * SR * 3:(i + 1) * SR * 3], mel).astype(np.float32)
            e = sess.run(["embs"], {"audio_signal": f[None, ...],
                                    "length": np.array([f.shape[1]], np.int64)})[0][0]
            out.append(e / np.linalg.norm(e))
        segs[p.stem] = out

    names = list(segs)
    same = [float(segs[n][i] @ segs[n][j])
            for n in names for i in range(6) for j in range(i + 1, 6)]
    diff = [float(a @ b) for a in segs[names[0]] for b in segs[names[1]]]
    margin = min(same) - max(diff)
    print(f"  same  mean {np.mean(same):.3f}  min {min(same):.3f}")
    print(f"  diff  mean {np.mean(diff):.3f}  max {max(diff):.3f}")
    print(f"  margin {margin:+.3f}")
    if margin <= 0:
        print("  FAIL: the distributions overlap — no threshold can separate them")
        return False
    print("  ok")
    return True


if __name__ == "__main__":
    sys.exit(main())
