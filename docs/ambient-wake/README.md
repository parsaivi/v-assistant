# The Ambient Wake Gate

*Design note and implementation record. 2026-08-24.*

The phone listens all the time and forwards nothing until it has heard the wake
word **in Parsa's voice**. Then it opens a conversation that runs turn to turn
without a wake word between turns, shows itself on screen for as long as the
microphone is open, and closes on silence or on being told to.

Everything that decides is a small neural network running on the phone. None of
it is a language model, none of it costs anything, and no audio crosses the
mutual-TLS link to the laptop until both gates have passed.

---

## 1. What this replaced, and why the trigger moved

The first version triggered on a **Bluetooth headset connecting**: headset
connects → V says "Yes?" → records one turn → done. That is gone.

The reason is a property of the radio, not a preference. **Classic Bluetooth has
no microphone in A2DP.** The only mic path is HFP/SCO, and opening SCO tears
A2DP down — audio drops to narrowband mono and the headset flips into call mode.
So "listen through the earbuds while music plays" is not a thing the hardware
can do. Holding SCO open for hours also drains both batteries and leaves the
phone stuck in communication mode if the process dies mid-turn.

Listening on the **phone's own microphone** instead makes the headset irrelevant
to whether V can hear, which is why the connection is no longer a trigger at all.
What that bought:

| | |
|---|---|
| Music | untouched, full A2DP quality, ducked only while V speaks |
| Routing | nothing to decide — connected headphones already get the reply |
| Enrollment | **one** channel, not two |
| Deleted | ~60 lines of SCO state machine, 3 Bluetooth permissions, the 1.2 s SCO settle delay that used to clip the first syllable |

That third row is the one that matters most and is easiest to miss. A speaker
centroid is only comparable to embeddings computed on the same channel. A design
that listened on both SCO and the phone mic would need two enrollments and would
silently use the wrong one whenever a headset connected mid-session.

Which creates an inversion worth stating plainly: `Recorder` originally chose
`VOICE_RECOGNITION` **because** that source follows a Bluetooth headset when one
connects. That is now precisely wrong, so `Gate.pin()` and `Recorder` both nail
the input to `TYPE_BUILTIN_MIC`. The headset is not ignored passively; it is
ignored *deliberately*.

---

## 2. The pipeline

Three models in series, cheapest first, each one only reached if the previous
one had a reason to run it.

```
mic ──► Silero VAD ──► utterance segmentation ──► openWakeWord ──► TitaNet ──► conversation
        every 32ms        (VAD-delimited)          templates       centroid
        ~0.3 ms           free                     ~15 ms          ~25 ms
        rejects a          rejects most             rejects          rejects
        quiet room         of speech                other words      other people
```

**Why VAD first.** An RMS threshold was tried and is not enough: a fan, a
keyboard and traffic through a window all clear any threshold that a quiet
sentence also clears. Silero distinguishes speech from *loud*, which is the
distinction that decides whether the expensive stages ever run. In a quiet room
it rejects essentially every frame for a third of a millisecond each.

**Why segments, not a sliding window.** The gate scores VAD-delimited utterances
rather than sweeping the matcher over a rolling window every 80 ms. It costs up
to 320 ms of latency after he stops speaking and buys three things: the matcher
sees complete words aligned the way the enrollment samples were, it runs about
twenty times an hour instead of forty thousand, and a segment has a *duration* —
which rejects most of a conversation before any model loads.

A long segment gets its head tested too, so "Friday, what's on today" said in one
breath still matches on the word at the front of it.

---

## 3. Choosing the speaker model — the part that was measured

This was decided by experiment, and the losing candidate is worth recording
because it failed in a way that looks like success.

Test: two speakers from `~/.cache/v/models/voices` (216 Hz and 134 Hz median F0 —
clearly different people), six 3-second segments each, cosine similarity within
and across speakers.

| Model | same-speaker | different-speaker | margin |
|---|---|---|---|
| WeSpeaker CAM++ (LM), 512-d | mean 0.922 | mean 0.861 | **−0.070** |
| **NeMo TitaNet-small, 192-d** | mean 0.794, min 0.691 | mean 0.262, max 0.405 | **+0.286** |

CAM++'s distributions **overlap**. There is no threshold that admits Parsa and
refuses everyone else. Four preprocessing variants were swept (int16 vs ±1
scaling, with and without cepstral mean normalisation) and all four failed the
same way.

The cause was visible in the embeddings themselves: the global mean vector had
norm 121.6 against a per-embedding norm of 128.3, so **95 % of every CAM++
vector was a constant shared by all utterances** and cosine was mostly measuring
that constant. Subtracting a global mean fixes it — the means separate by 0.61 —
but that mean has to come from a corpus the phone does not have and cannot
compute from one speaker. TitaNet needs no such correction.

TitaNet-small ships as `models/speaker.onnx` with its 16 681-class classifier
head stripped via `onnx.utils.extract_model` (40.3 MB → **27.5 MB**), verified to
produce identical embeddings after stripping.

> **If accuracy ever needs improving**, the next thing to try is `nemo_en_titanet_large`
> or int8 dynamic quantisation of the current model — not CAM++ with a bundled
> global mean, which is a lot of machinery to reach a worse starting point.

---

## 4. The feature front-end, and why it is the riskiest file

`Features.kt` is the highest-risk code here, because of the specific way it
fails. A wrong filterbank, an off-by-one in the reflect padding, or a symmetric
hann window where librosa's is periodic all produce features that look
completely reasonable — finite, smoothly varying, correctly shaped — and hand
the speaker model something it has never seen. It returns 192 confident floats
that encode nothing. Nothing throws. The only symptom is that V stops
recognising Parsa, months later, with no error to read.

Two decisions exist purely to remove chances to be wrong:

1. **The mel filterbank is shipped, not computed.** `models/mel80.f32` is the
   exact 80×257 matrix from `librosa.filters.mel(norm="slaney")`, dumped to
   float32. Slaney-normalised mel has two breakpoint conventions and an
   area-normalisation step that are easy to get subtly wrong. 82 KB buys
   certainty.
2. **Everything else mirrors NeMo's featurizer step for step**, because TitaNet
   was trained on it: pre-emphasis 0.97 → STFT(512, hop 160, win 400, periodic
   hann, reflect-padded) → power → mel → `log(x + 2⁻²⁴)` → per-feature mean/variance
   normalisation over time.

**It is verified rather than asserted.** `FeaturesTest` runs the Kotlin against
golden vectors generated by the Python pipeline that produced the +0.286 margin,
on a deterministic three-sinusoid signal both sides compute identically. Maximum
deviation must stay under 2 × 10⁻³. That test runs on the JVM — no emulator, no
phone — so the riskiest code in the gate is checked by `gradle test`.

---

## 5. The wake word: templates, not a trained detector

The wake matcher uses openWakeWord's feature extractor — Google's
`speech_embedding` network, `mel.onnx` → `emb.onnx`, mapping 760 ms to 96
dimensions that encode *what was said* rather than who said it — but **not** a
trained openWakeWord classifier. It compares against templates recorded at
enrollment.

That split is the design: this stage answers "was that the word", `SpeakerId`
answers "was that him", and neither is asked to do the other's job.

Training a real detector is more robust to noise and is what should replace this
if the shadow-mode numbers ever justify it. Three things argued against doing it
first:

1. It needs a GPU training run before a single line of the app can be tested end
   to end, and it has to be redone to change the word.
2. Those models train on English TTS. Parsa says "Friday" with a Persian vowel in
   it, and a detector trained on American synthetic speech is worst exactly where
   it matters most.
3. Enrollment already records fifteen "Friday"s in his own voice, because the
   speaker check needs them. They describe the sound he actually makes better
   than synthetic data does, and cost nothing extra.

Descriptors concatenate mean- and max-pooled embeddings. Max pooling is there
because "Friday" is two syllables with a distinctive stop in the middle, and mean
pooling alone blurs the sequence into an average that "fry day", "privacy" and
"Thursday" all sit near.

> **On the word itself.** "V" was the other candidate and was rejected: a single
> phoneme, /viː/, fires on *we*, *be*, *he*, *see*, *three*. The speaker check
> cannot save it, because it is Parsa's voice saying those words too. "Friday" is
> three syllables and distinctive — and, usefully, it is already an agent in
> `persona.py` with her own memory scope, so the wake word selects **who
> answers** rather than labelling a reply from V.

---

## 6. Enrollment

Fifteen takes of "Friday", recorded through **the same path the gate listens
on** — same source, same pinned built-in mic, same VAD segmentation, same
16 kHz. A voiceprint captured through a different channel than it is verified
against is the classic way this feature ships broken: the enrollment sounds
fine, the numbers look fine, and the thresholds are calibrated against audio the
gate will never see.

`Enrollment.commit()` **throws samples away**. Of fifteen takes some will be a
cough, a clipped first syllable, or the microwave, and averaging those into the
centroid moves it toward the room and away from him — which loosens every future
comparison in the direction of accepting strangers. Outliers are found against
the **median** pairwise cosine, not the mean, because the mean is exactly what a
bad sample drags; using it to detect bad samples would let one cough hide itself.
Rejection is at 3 × MAD, and the screen reports how many were dropped so a bad
enrollment is visible rather than merely worse.

The voiceprint is stored Keystore-wrapped via `EncryptedSharedPreferences`. It is
biometric data, it never leaves the phone, and it is not in a backup — the same
argument the app's private key gets in the manifest, with more force, because a
key can be rotated.

---

## 7. Shadow mode — how the thresholds actually get set

**The gate ships with `shadowMode = true` and it should stay that way for about
a week.** In shadow mode it scores every candidate utterance and acts on none of
them, writing `(timestamp, duration, wake score, speaker score)` — scores only,
never audio — to a 400-entry ring buffer.

This is not caution for its own sake. Two gates in series *multiply* false
rejections, and the starting values (`wake 0.62`, `speaker 0.55`) are guesses
wearing numbers' clothes. Setting them against the distribution of scores from
the room Parsa actually sits in is the only honest way, and it is why the
thresholds live in storage rather than as constants in a source file.

`CalibrateScreen` is where that happens, and it opens by itself while the gate
is still in shadow mode — a gate that scores everything and acts on nothing is
not finished being set up. Afterwards it lives behind a long-press on the
header's metadata line.

It offers two ways to set the dials, and the second is the one that actually
works:

1. **The log**, drawn as two strips of dots on a 0–1 axis with the threshold as
   a line. Not a histogram: binning would smooth over exactly the overlap you
   are looking for. The headline is *"would fire: n of m"* — threshold-setting
   is not an abstract judgement about cosine similarity, it is the question of
   how much of what the phone already heard gets through.
2. **A live probe.** Press, say "Friday", and see the two scores that utterance
   produced and whether it would have fired. A week of logged numbers tells you
   the distributions but never which entry was which; this closes the loop in
   one second. `Enrollment.probe` deliberately mirrors `Gate.evaluate` — same
   head-trimming, same padding — because a probe measuring something slightly
   different would be worse than no probe.

The procedure:

1. Enroll. Use the phone normally for a week, saying "Friday" out loud sometimes.
2. Open calibration. Probe a few times to see where a real "Friday" lands.
3. Set the wake threshold below your probes and above the noise; raise the
   speaker threshold until other voices stop clearing it.
4. Press **Start acting on these**.

The known weak spot, stated in advance because it is where this will fail first:
**the wake segment is short.** "Friday" is around 700 ms, and the +0.286 margin
was measured on 3-second segments. The gate pads short segments from a 4-second
rolling buffer to claw some of that back. Whether that is enough is exactly what
shadow mode is for.

---

## 8. The conversation

Wake → "Yes?" → listen → answer → listen → … until either nobody speaks for 8 s
or he says a closing phrase.

**No wake word between turns.** That is the whole difference between this and a
voice assistant — having to say "Friday" before every sentence is what makes
those feel like vending machines. Closing phrases (`"that's all"`, `"thanks
Friday"`, …) are matched by string comparison against the transcript Whisper
already returned, so ending a conversation costs nothing and cannot fail
differently than the transcript did.

`/v1/wake` still runs no model and says one short thing. It now takes an `agent`
parameter, routed through `Engine.switch_agent` rather than assigning
`engine.persona` directly — that is what rebuilds the memory store on the new
scope and clears the previous agent's shell thread. Assigning the field would
give Friday V's beliefs and V's transcript.

### It shows on screen

When the gate fires, `VoiceScreen` comes up over whatever was there, including
the lock screen. It is the brief's Ambient surface rendered on a phone, and it is
there for a reason beyond looking finished:

**An open microphone must be visible.** The session holds the mic for up to eight
seconds after V stops talking, with no wake word needed to continue. That is the
right interaction, and it is also precisely what people fear a device like this
is doing behind their back. So while it is true, it is on screen at full
brightness saying which state it is in, and one tap ends it. The status-bar mic
dot says *something* is recording; this says what, and offers a way out.

Starting an activity from a service has been restricted since Android 10.
`SYSTEM_ALERT_WINDOW` is the exemption, requested once in Settings after
enrollment. Without it the conversation still runs — it just arrives as a
notification instead of a window.

---

## 9. Platform constraints that always-on creates

Listening was previously bounded to headphone sessions. 24/7 surfaces four
things:

1. **Foreground service type.** From Android 14 a service that records must
   declare `microphone` and hold `FOREGROUND_SERVICE_MICROPHONE`. Done, as
   `microphone|dataSync`.
2. **Boot cannot arm the mic.** `microphone` is a *while-in-use* type, and
   `BOOT_COMPLETED` — an exemption for starting a foreground service in general
   — is **not** an exemption for that type. So `BootReceiver` starts the service
   **disarmed**: the event stream comes back, so a missed reminder still
   arrives, and the gate waits until `MainActivity` next runs. One tap after a
   reboot, versus a service that crashes on boot forever. `foreground()` also
   catches the refusal and degrades to data-sync rather than letting the whole
   service die and take the reminder channel with it.
3. **The green dot is permanent.** Android 12+ shows a mic indicator whenever the
   mic is live. Always-on means it never goes away. Nothing can remove it.
4. **Microphone contention.** Holding the mic forever means competing with calls,
   voice notes, the camera. `Gate.session()` treats losing the mic as a *pause*,
   not an error — completely ordinary — and retries every 1.5 s until it is free.

And a fifth that is not a constraint but a hazard: `yieldMic()` only raises a
flag, and the reader is blocked inside `AudioRecord.read` for up to one buffer
after. Two `AudioRecord`s on one microphone is **not** an error Android reports —
the second simply receives silence. `VService.onWake` therefore waits for the
gate to leave `LISTENING` before the conversation opens its own recorder, or the
first turn of every conversation would be empty.

---

## 10. What is here

```
android/app/src/main/assets/models/
  vad.onnx        2.3 MB   Silero VAD v5
  mel.onnx        1.1 MB   openWakeWord melspectrogram
  emb.onnx        1.3 MB   Google speech_embedding
  speaker.onnx   27.5 MB   TitaNet-small, classifier head stripped
  mel80.f32        82 KB   librosa slaney mel filterbank

android/app/src/main/java/cloud/brain/v/
  Features.kt      NeMo log-mel front-end + FFT        ← verified by FeaturesTest
  Onnx.kt          shared runtime, one thread per session
  Vad.kt           stage 1
  WakeWord.kt      stage 2 — template matching
  SpeakerId.kt     stage 3 — TitaNet embedding
  Voiceprint.kt    Keystore-wrapped storage, thresholds, shadow log
  Gate.kt          mic loop, segmentation, the decision
  Mic.kt           the microphone lease — one holder, arbitrated
  BargeIn.kt       interruption watcher, with echo cancellation
  Enrollment.kt    capture + outlier rejection + the live probe
  EnrollScreen.kt  the one screen that had to exist
  CalibrateScreen.kt  the shadow log, two dials, and the probe
  VoiceSession.kt  the multi-turn conversation
  VoiceScreen.kt   the Ambient surface, shown while the mic is open
  VService.kt      always-on gate (was: headset receiver)

src/v/core/httpapi.py   `/v1/wake` takes `agent`
```

APK: **62 MB** (32 MB models, 18.5 MB onnxruntime arm64). ABI filtered to
`arm64-v8a`; the AAR ships four ABIs and every phone this will run on is arm64.

### Tests

- `FeaturesTest` — 4 tests, JVM, golden vectors vs the Python reference
- `GateLogicTest` — 7 tests, JVM: outlier rejection and close-phrase matching.
  The second caught a real bug — the original rule also matched closers as a
  *prefix*, so "that's all right, what about Tuesday" hung up mid-question.
- `test_httpapi` — 27 tests, 3 new: wake is free and says one thing; the wake
  word chooses who answers; an unknown agent still answers

---

## 11. Barge-in, and the echo problem

Talking over a long reply stops it. The naive version does not work: the mic
hears V's own voice from the speaker, the VAD says "speech", and V interrupts
himself about 200 ms into every answer. Acoustic echo is not an edge case here,
it is the default outcome.

Three things stacked against it, in order of how much they matter:

1. **`VOICE_COMMUNICATION` as the source** — the one input Android runs its
   platform echo canceller on. Note this differs from the gate's
   `VOICE_RECOGNITION`; the two are never open at once, so they never disagree
   about the device.
2. **`AcousticEchoCanceler` explicitly**, where the platform exposes it as a
   separate effect. Availability is per-device and cannot be assumed.
3. **A sustained-speech requirement** — 220 ms of continuous speech with a
   canceller, 500 ms without. Residual echo produces short VAD blips; this
   removes essentially all of them.

Where no canceller exists the bar is raised rather than the feature disabled. On
a phone playing loudly through its speaker it may still occasionally self-trigger,
and that is an acceptable failure: the cost is one reply cut short, against every
long answer having to be sat through.

## 12. Not done

- **A trained wake detector.** See §5 — deliberate, revisit after shadow mode.
- **Nothing has run on a physical phone.** Everything here compiles, the APK
  builds, the feature front-end is verified against golden vectors and the server
  tests pass — but no audio has gone through this on-device. The numbers in §3
  come from desktop ONNX Runtime on real speech; on-device latency and battery
  are estimates until measured.
