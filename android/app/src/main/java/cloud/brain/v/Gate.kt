package cloud.brain.v

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext
import kotlin.math.min
import kotlin.math.sqrt

/**
 * The always-on gate: hears everything, forwards nothing until it is sure.
 *
 * This is the piece the whole feature exists for, and its contract is one
 * sentence — **no audio leaves this class unless the wake word matched and the
 * voice was Parsa's.** Everything below serves that, including the parts that
 * look like premature optimisation.
 *
 * ### Why it listens on the built-in mic, always
 *
 * The first version of this triggered on a headset connecting and recorded
 * through the headset. That is gone. Classic Bluetooth has no microphone in
 * A2DP — the only mic path is HFP/SCO, and opening SCO tears down A2DP, so
 * "listen through the earbuds while music plays" is not a thing the radio can
 * do. Listening on the phone instead means music is untouched, no SCO state
 * machine exists to get wrong, and there is one audio channel rather than two.
 *
 * That last point is not a convenience. A speaker centroid is only comparable
 * to embeddings from the same channel; 8kHz SCO audio and 16kHz phone audio
 * produce measurably different embeddings for the same person, so a design
 * with both would need two enrollments and would silently use the wrong one
 * whenever a headset connected mid-session. Hence [pin] — the input device is
 * explicitly nailed to the built-in mic rather than merely left alone, because
 * `VOICE_RECOGNITION` will otherwise follow a headset when one appears.
 *
 * ### Why segments, not a sliding window
 *
 * The gate scores VAD-delimited utterances rather than running the matcher over
 * a rolling window every 80ms. It costs up to [HANGOVER_MS] of latency after he
 * stops speaking, and buys three things: the matcher runs on complete words
 * aligned the way the enrollment samples were, it runs perhaps twenty times an
 * hour instead of forty thousand, and a segment has a duration — which rejects
 * most of a conversation before any model runs at all.
 */
class Gate(private val context: Context, private val prints: Voiceprint) {

    companion object {
        const val RATE = 16_000
        private const val HANGOVER_MS = 320      // silence that ends an utterance
        private const val PAD_MS = 700           // context added around a wake segment
        private const val BUFFER_MS = 4_000      // rolling history kept for padding
        /** A segment longer than this is a sentence, not a wake word; only its
         *  opening is worth testing, in case he ran "Friday, what's on" together. */
        private const val HEAD_MS = 1_400
    }

    /**
     * [SILENCED] is not the same as [NO_MIC], and conflating them is how this
     * feature fails invisibly. `NO_MIC` means the device could not be opened —
     * ordinary, transient, another app has it. `SILENCED` means it opened and
     * is returning exact zeroes, which is what Android does instead of refusing
     * an app that lacks the permission or has been muted by an OS toggle. The
     * gate looks perfectly healthy in that state and can never fire, so it has
     * to be a state the notification can name.
     */
    enum class State { OFF, LISTENING, HEARD, YIELDED, NO_MIC, SILENCED }

    private val _state = MutableStateFlow(State.OFF)
    val state: StateFlow<State> = _state

    /** Live RMS 0..1 for the aperture's waveform, so the UI shows the room. */
    private val _level = MutableStateFlow(0f)
    val level: StateFlow<Float> = _level

    private var vad: Vad? = null
    private var wake: WakeWord? = null
    private var speaker: SpeakerId? = null

    @Volatile private var yielded = false

    /** Stop scoring and drop the mic — the conversation needs it. */
    fun yieldMic() { yielded = true }
    fun resumeMic() { yielded = false }

    /** Parked for either reason: a conversation took over, or a screen did.
     *  Both mean the same thing to the loop — let go and wait. */
    private val parked: Boolean get() = yielded || !Mic.gateMayListen

    /**
     * Run until cancelled. [onWake] fires on the calling coroutine's dispatcher
     * with the audio that triggered it, and only ever after both gates passed.
     */
    @SuppressLint("MissingPermission")
    suspend fun run(onWake: suspend (FloatArray) -> Unit) = withContext(Dispatchers.Default) {
        Features.load(context)
        val v = Vad(context).also { vad = it }
        val w = WakeWord(context).also { wake = it }
        val s = SpeakerId(context).also { speaker = it }
        try {
            while (coroutineContext.isActive) {
                if (parked) {
                    _state.value = State.YIELDED
                    kotlinx.coroutines.delay(200)
                    continue
                }
                val opened = session(v, w, s, onWake)
                // `session` returns when the mic was lost or yielded. Another
                // app taking the microphone is completely ordinary — a call, a
                // voice note, the camera — so it is a pause, not an error, and
                // the loop simply waits for it back.
                if (!opened) {
                    _state.value = State.NO_MIC
                    kotlinx.coroutines.delay(1_500)
                }
            }
        } finally {
            _state.value = State.OFF
            Mic.gateHolds(false)
            v.close(); w.close(); s.close()
            vad = null; wake = null; speaker = null
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun session(
        v: Vad, w: WakeWord, s: SpeakerId, onWake: suspend (FloatArray) -> Unit,
    ): Boolean {
        val minBuf = AudioRecord.getMinBufferSize(
            RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return false

        val record = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        maxOf(minBuf * 4, Vad.WINDOW * 16))
        } catch (e: Exception) {
            return false
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) { record.release(); return false }
        pin(record)

        v.reset()
        val history = RingBuffer(RATE * BUFFER_MS / 1000)
        val segment = ArrayList<Short>(RATE * 2)
        var inSpeech = false
        var quietMs = 0
        var silentMs = 0
        var everNonZero = false
        val frame = ShortArray(Vad.WINDOW)
        val window = FloatArray(Vad.WINDOW)

        try {
            record.startRecording()
            _state.value = State.LISTENING
            Mic.gateHolds(true)
            while (coroutineContext.isActive && !parked) {
                var got = 0
                while (got < Vad.WINDOW) {
                    val n = record.read(frame, got, Vad.WINDOW - got)
                    if (n <= 0) return false          // mic taken, or dead
                    got += n
                }
                history.write(frame)

                // A real room dithers the low bit; an unbroken run of exact
                // zeroes is the OS silencing us, not quiet.
                if (!everNonZero) {
                    if (frame.any { it.toInt() != 0 }) {
                        everNonZero = true
                    } else {
                        silentMs += Vad.WINDOW * 1000 / RATE
                        if (silentMs > 3_000) {
                            _state.value = State.SILENCED
                            return false
                        }
                    }
                }

                var sum = 0.0
                for (i in 0 until Vad.WINDOW) {
                    window[i] = frame[i] / 32768f
                    sum += window[i] * window[i]
                }
                _level.value = min(1.0, sqrt(sum / Vad.WINDOW) * 12).toFloat()

                v.probability(window)
                val ms = Vad.WINDOW * 1000 / RATE

                if (v.isSpeaking) {
                    if (!inSpeech) { inSpeech = true; segment.clear() }
                    quietMs = 0
                    for (x in frame) segment.add(x)
                    // A monologue is not a wake word. Cap the buffer so a long
                    // sentence cannot grow it without bound.
                    if (segment.size > RATE * 8) { inSpeech = false; segment.clear() }
                } else if (inSpeech) {
                    for (x in frame) segment.add(x)
                    quietMs += ms
                    if (quietMs >= HANGOVER_MS) {
                        inSpeech = false
                        evaluate(segment.toShortArray(), history, w, s, onWake)
                        segment.clear()
                    }
                }
            }
            return true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            return false
        } finally {
            // Cleared before the handle is released, so anything waiting on it
            // cannot wake up while this still owns the device.
            Mic.gateHolds(false)
            try { record.stop() } catch (e: Exception) { /* already stopped */ }
            record.release()
            _level.value = 0f
        }
    }

    /**
     * One candidate utterance. This is the only place a decision is made.
     */
    private suspend fun evaluate(
        seg: ShortArray, history: RingBuffer,
        w: WakeWord, s: SpeakerId, onWake: suspend (FloatArray) -> Unit,
    ) {
        if (seg.size < WakeWord.MIN_SAMPLES) return
        // Test the head of a long segment too: "Friday, what's on today" is one
        // VAD segment, and the word is only at the front of it.
        val head = if (seg.size > RATE * HEAD_MS / 1000)
            seg.copyOfRange(0, RATE * HEAD_MS / 1000) else seg
        if (head.size > WakeWord.MAX_SAMPLES && seg.size > WakeWord.MAX_SAMPLES) return

        val int16 = FloatArray(head.size) { head[it].toFloat() }
        val descriptor = w.describe(int16) ?: return
        val wakeScore = w.score(descriptor, prints.templates())

        val shadow = prints.shadowMode
        if (wakeScore < prints.wakeThreshold && !shadow) return

        // Speaker check on the same audio, padded from history — a 700ms word
        // is thin evidence of identity and the surrounding second helps.
        val padded = history.tail(maxOf(head.size + RATE * PAD_MS / 1000,
                                        SpeakerId.MIN_SAMPLES))
        val norm = FloatArray(padded.size) { padded[it] / 32768f }
        val speakerScore = prints.centroid()?.let { c ->
            Features.cosine(s.embed(norm), c)
        }

        prints.record(head.size * 1000 / RATE, wakeScore, speakerScore)

        if (shadow) return
        if (wakeScore < prints.wakeThreshold) return
        if (speakerScore == null || speakerScore < prints.speakerThreshold) return

        _state.value = State.HEARD
        onWake(norm)
    }

    /**
     * Nail the input to the phone's own microphone.
     *
     * `VOICE_RECOGNITION` was chosen originally *because* it follows a
     * bluetooth headset when one is connected. That is now precisely wrong: if
     * the input device changes underneath a running gate, the acoustics change,
     * and a threshold calibrated on the phone mic starts rejecting him halfway
     * through the afternoon for no visible reason.
     */
    private fun pin(record: AudioRecord) {
        try {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am.getDevices(AudioManager.GET_DEVICES_INPUTS)
                .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
                ?.let { record.setPreferredDevice(it) }
        } catch (e: Exception) {
            // Not fatal — the default route is still a microphone. Worth
            // attempting rather than requiring.
        }
    }
}

/** Fixed-size history of recent audio, so a wake segment can be padded with
 *  what came before it without keeping the whole day in memory. */
private class RingBuffer(private val capacity: Int) {
    private val buf = ShortArray(capacity)
    private var head = 0
    private var filled = 0

    fun write(src: ShortArray) {
        for (x in src) {
            buf[head] = x
            head = (head + 1) % capacity
            if (filled < capacity) filled++
        }
    }

    /** The most recent [n] samples, oldest first. */
    fun tail(n: Int): ShortArray {
        val take = min(n, filled)
        val out = ShortArray(take)
        var i = (head - take + capacity) % capacity
        for (k in 0 until take) { out[k] = buf[i]; i = (i + 1) % capacity }
        return out
    }
}
