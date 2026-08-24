"""Tests for text-to-speech (`core/tts.py`).

Run:  python3 -m v.tests.test_tts

Mirrors `test_telegram.py`'s posture on the STT side: no pytest, a throwaway
vault per test. The end-to-end synthesis test uses the real Piper voice this
project ships in `~/.cache/v/models/piper/` when it's present and quietly
skips the audio assertions when it isn't — same zero-cost-by-default stance
as the rest of the suite, since the voice model is an optional download, not
a repo file.
"""

from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import persona, tts
from v.core.config import settings as default_settings
from v.tests.test_core import make_vault

PASS, FAIL = [], []


def test(fn):
    def run():
        try:
            fn()
            PASS.append(fn.__name__)
            print(f"  \x1b[32m✓\x1b[0m {fn.__name__}")
        except Exception as exc:
            FAIL.append((fn.__name__, exc, traceback.format_exc()))
            print(f"  \x1b[31m✗\x1b[0m {fn.__name__}: {exc}")
    run.fn = fn
    return run


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


# ---------------------------------------------------------------------
@test
def a_missing_voice_model_degrades_to_unavailable_not_a_crash():
    """A throwaway test vault has no `models/piper/` next to it — this is
    the shape a machine with piper-tts installed but no voice downloaded
    yet is in, and it must raise the typed `Unavailable`, not blow up on a
    missing file."""
    root, s = make_vault()
    try:
        synth = tts.Synthesizer(s, voice="en_GB-alan-medium")
        eq(synth.available, False)
        try:
            synth.load()
            raise AssertionError("load() must raise Unavailable, not succeed")
        except tts.Unavailable as exc:
            ok("en_GB-alan-medium" in str(exc), "the error should name the voice")
    finally:
        shutil.rmtree(root)


@test
def wav_to_ogg_opus_produces_a_valid_ogg_container():
    """Doesn't need Piper at all — just a WAV in, an OGG out, so this runs
    everywhere ffmpeg does (already a hard dependency via `stt.convert_to_wav`)."""
    import wave
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)  # 1s of silence
    ogg = tts.to_ogg_opus(buf.getvalue())
    ok(len(ogg) > 0, "conversion must produce non-empty output")
    eq(ogg[:4], b"OggS", "must be a real OGG container, not passthrough")


@test
def the_shipped_british_voice_synthesizes_real_audio_when_installed():
    """End-to-end against the actual voice this project ships with — skips
    its assertions (not a failure) on a machine that hasn't downloaded the
    model, same posture the rest of the suite gives optional local models."""
    synth = tts.Synthesizer(default_settings, voice="en_GB-alan-medium")
    if not synth.available:
        return  # nothing to assert without the model — not a failure
    wav = synth.synthesize("Good evening, sir.")
    ok(wav[:4] == b"RIFF", "must be a real WAV file")
    ok(len(wav) > 1000, "a real sentence must produce more than a few bytes")
    ogg = tts.to_ogg_opus(wav)
    eq(ogg[:4], b"OggS")


@test
def segments_splits_by_script_and_folds_whitespace_into_neighbors():
    """A space between an English word and a Persian word must not become
    its own run — it should stick to one side or the other, or a mixed
    sentence turns into three tiny synth calls instead of two."""
    chunks = tts._segments("Read your note titled سلام دنیا today.")
    eq([c for c, _ in chunks],
       ["Read your note titled ", "سلام دنیا ", "today."])
    eq([fa for _, fa in chunks], [False, True, False])


@test
def mixed_synthesizer_falls_back_to_english_when_persian_voice_missing():
    """A throwaway vault has neither voice downloaded — Persian runs must
    fall back to the (also missing) English voice's `Unavailable`, not to
    some third code path, so the failure mode stays the one path callers
    already handle."""
    root, s = make_vault()
    try:
        m = tts.MixedSynthesizer(s)
        eq(m.available, False)
        eq(m.fa.available, False, "no fa model in a throwaway vault either")
    finally:
        shutil.rmtree(root)


@test
def mixed_synthesizer_produces_real_audio_when_both_voices_are_installed():
    """End-to-end against the two voices this project ships with — skips
    (not fails) on a machine missing either model."""
    m = tts.MixedSynthesizer(default_settings)
    if not (m.en.available and m.fa.available):
        return
    wav = m.synthesize("Read your note titled سلام دنیا today.")
    ok(wav[:4] == b"RIFF", "must be a real WAV file")
    ok(len(wav) > 1000, "a mixed sentence must produce more than a few bytes")


# ---- the hosted voice, and the one underneath it ---------------------
@test
def the_voice_comes_from_the_persona_not_the_config():
    """`tts_voice` is an override and empty by default, so a fresh vault has
    to get its voices from whichever character is configured. If this ever
    resolves to "" the Piper path looks for `.onnx` and reports a missing
    model, which is a confusing way to say "nobody chose a voice"."""
    root, s = make_vault()
    try:
        p = persona.get(s.persona)
        eq(tts.Synthesizer(s).voice, p.spoken.piper)
        eq(tts.MixedSynthesizer(s).fa.voice, p.spoken.piper_fa)
        r = tts.RemoteSynthesizer(s)
        eq((r.model, r.voice), (p.spoken.model, p.spoken.voice))
        # And the override still overrides.
        s.tts_remote_voice = "gb_jane_neutral"
        eq(tts.RemoteSynthesizer(s).voice, "gb_jane_neutral")
    finally:
        shutil.rmtree(root)


@test
def a_missing_key_is_unavailable_and_says_what_to_do():
    root, s = make_vault()
    try:
        s.openrouter_api_key = ""
        r = tts.RemoteSynthesizer(s)
        ok(not r.available, "no key means not available")
        try:
            r.synthesize("Good evening.")
        except tts.Unavailable as exc:
            ok("OPENROUTER_API_KEY" in str(exc), "the error should say what to set")
        else:
            raise AssertionError("a keyless remote synthesizer must not pretend")
    finally:
        shutil.rmtree(root)


@test
def the_hosted_voice_failing_costs_the_accent_and_not_the_reply():
    """The whole reason `Fallback` exists. A remote failure has to end in
    Piper's audio, not in silence — Telegram catches `Unavailable` by sending
    no voice note at all, so anything that escapes here is a mute V."""
    class Dead:
        available = True

        def load(self):
            return self

        def synthesize(self, text):
            raise tts.Unavailable("402 — out of credits")

    class Local:
        available = True
        said = []

        def load(self):
            return self

        def synthesize(self, text):
            self.said.append(text)
            return b"RIFFfake"

    local = Local()
    seen = []
    f = tts.Fallback(Dead(), local, on_fallback=seen.append)
    eq(f.load(), f, "a dead primary must not stop the pair from loading")
    eq(f.synthesize("Good evening, sir."), b"RIFFfake")
    eq(local.said, ["Good evening, sir."])
    ok("402" in f.last_error, "the reason has to survive for the log line")
    eq(len(seen), 1, "the surface is told once, so it can print it")

    # A bug in the remote path, rather than a clean Unavailable, must also
    # land on Piper — this is the case that would otherwise crash the reply.
    class Broken(Dead):
        def synthesize(self, text):
            raise ValueError("some parsing bug")

    eq(tts.Fallback(Broken(), local).synthesize("again"), b"RIFFfake")


@test
def the_factory_reads_the_provider_and_null_still_means_off():
    root, s = make_vault()
    try:
        s.tts_provider = "openrouter"
        m = tts.synthesizer(s)
        ok(isinstance(m, tts.Fallback), "the hosted voice keeps piper underneath")
        ok(isinstance(m.backup, tts.MixedSynthesizer))
        s.tts_provider = "piper"
        ok(isinstance(tts.synthesizer(s), tts.MixedSynthesizer))
        s.tts_provider = "null"
        off = tts.synthesizer(s)
        ok(not off.available)
        try:
            off.synthesize("anything")
        except tts.Unavailable:
            pass
        else:
            raise AssertionError("null must refuse rather than speak")
    finally:
        shutil.rmtree(root)


@test
def a_repeated_line_is_spoken_once_and_replayed_after_that():
    """`/new` returns one constant sentence and `telegram._reply` speaks every
    reply, so without this each one is a paid round-trip for identical audio.

    The assertion is on the *call count*, not on the timing: a cache that
    happens to be fast but still hits the network fixes the latency and none
    of the bill, which is the half Parsa actually asked about.
    """
    root, s = make_vault()
    try:
        s.openrouter_api_key = "test-key"
        calls = []

        class FakeResponse:
            headers = {}

            def read(self):
                return b"ID3fake-mp3-bytes"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return FakeResponse()

        real = tts.urllib.request.urlopen
        tts.urllib.request.urlopen = fake_urlopen
        try:
            r = tts.RemoteSynthesizer(s)
            r.reference = ""          # a preset voice: no clip to find on disk
            r.voice = "gb_oliver_confident"
            eq(r.synthesize("Starting fresh, sir."), b"ID3fake-mp3-bytes")
            eq(len(calls), 1, "the first time has to reach the provider")
            eq(r.synthesize("Starting fresh, sir."), b"ID3fake-mp3-bytes")
            eq(len(calls), 1, "the second time must not")

            # A different sentence is a different key, not a stale hit.
            r.synthesize("Good evening, sir. 2 reminders pending.")
            eq(len(calls), 2)

            # A long answer is unique by nature; caching it would only grow
            # the directory, so it is left alone in both directions.
            long_text = "x" * (tts.CACHE_MAX_CHARS + 1)
            r.synthesize(long_text)
            r.synthesize(long_text)
            eq(len(calls), 4, "long answers are not cached")

            # Switching persona must not replay the old voice.
            r.voice = "gb_jane_neutral"
            r.synthesize("Starting fresh, sir.")
            eq(len(calls), 5, "a different voice is a different cache entry")
        finally:
            tts.urllib.request.urlopen = real
    finally:
        shutil.rmtree(root)


@test
def replacing_a_reference_clip_invalidates_what_it_already_said():
    """The clip is fingerprinted by size and mtime, not by name — swapping in
    a better take of `evie.mp3` keeps the filename, and without this the new
    voice would only reach sentences nobody had heard yet."""
    root, s = make_vault()
    try:
        voices = s.model_dir / "voices"
        voices.mkdir(parents=True, exist_ok=True)
        clip = voices / "someone.mp3"
        clip.write_bytes(b"first take")

        r = tts.RemoteSynthesizer(s)
        r.reference, r.voice = "someone.mp3", ""
        before = r.ident()

        import time as _t
        _t.sleep(1.1)                     # mtime has one-second resolution
        clip.write_bytes(b"a longer second take")
        ok(r.ident() != before, "a replaced clip must produce a new cache key")
    finally:
        shutil.rmtree(root)


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
