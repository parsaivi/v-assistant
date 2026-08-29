package cloud.brain.v

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.min
import kotlin.math.sqrt

/**
 * The microphone, as raw PCM, and the level meter that falls out of it.
 *
 * `AudioRecord` rather than `MediaRecorder`, and WAV on the wire rather than a
 * compressed container, for two reasons that are really one:
 *
 *  1. **The waveform has to be real.** The brief is explicit — the 21 bars
 *     "must be driven by real mic RMS in production, not replayed". A
 *     `MediaRecorder` hands back a finished file and `getMaxAmplitude()`, which
 *     is one number with no history; raw frames give the actual signal, so the
 *     bars are the sound in the room rather than an animation that happens
 *     while a sound is in the room.
 *  2. **Silence has to be detectable.** The handsfree flow ends a turn when
 *     Parsa stops talking, not when a timer expires, and that needs the level
 *     while recording rather than after it.
 *
 * The cost is bandwidth — 16kHz mono PCM16 is 32KB/s, so a fifteen-second memo
 * is ~480KB — and on a link between a phone and a laptop in the same house,
 * that is not a cost. It also means the server never transcodes: 16kHz mono is
 * exactly what Whisper wants, so `stt.convert_to_wav` has nothing to do.
 */
class Recorder {

    companion object {
        const val RATE = 16_000            // what Whisper resamples to anyway
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        /** Below this RMS a frame counts as silence. Empirical, and generous —
         *  a room with a fan in it is not silent, and cutting Parsa off
         *  mid-sentence is a far worse failure than recording two seconds of
         *  nothing at the end. */
        private const val SILENCE_RMS = 320.0

        /** How long the level has to stay down before a handsfree turn ends. */
        private const val SILENCE_MS = 1_400

        /** A ceiling, so a headset left connected in a pocket cannot record
         *  until the battery dies. */
        private const val MAX_MS = 30_000

        /**
         * How much audio must pile up before a chunk is worth sending ahead.
         *
         * Matches `stt.MIN_CHUNK_SECONDS` on the server, and for the reason
         * written there: Whisper pads every call to a 30-second window, so the
         * encoder costs the same for one second of speech as for twenty and N
         * chunks means N passes. Measured, cutting a 3.2s clip into four
         * one-second pieces cost 8.1s of compute against 1.6s for one pass.
         *
         * So a short question — the common case — piles up less than this,
         * never emits an early chunk, and is transcribed exactly once when the
         * finger lifts. Only a long dictation, where there is real speaking
         * time to hide the work behind, gets cut. A 21-second memo becomes two
         * passes and the wait at the end is the tail alone.
         */
        private const val CHUNK_MIN_MS = 6_000

        /** Quiet this long is a phrase boundary — where a cut is safe.
         *  Shorter than [SILENCE_MS] because ending a *chunk* early is
         *  harmless while ending a *turn* early cuts him off. */
        private const val CHUNK_SILENCE_MS = 300

        /** The 21 bars of the design's waveform. */
        const val BARS = 21
    }

    /** The live level, 0..1 per bar, oldest first — what `Waveform` draws. */
    private val _levels = MutableStateFlow(FloatArray(BARS))
    val levels: StateFlow<FloatArray> = _levels

    @Volatile private var stopRequested = false

    fun requestStop() { stopRequested = true }

    /**
     * Record until [requestStop], or until [SILENCE_MS] of quiet once speech
     * has actually started when [untilSilence] is set.
     *
     * Returns a complete WAV. Returns null if the recorder could not open —
     * another app holding the mic, or a permission that was revoked between
     * the check and the call.
     */
    @SuppressLint("MissingPermission")
    suspend fun record(untilSilence: Boolean, giveUpMs: Int = 0,
                       context: android.content.Context? = null,
                       onChunk: (suspend (ByteArray, Boolean) -> Unit)? = null):
            ByteArray? = withContext(Dispatchers.IO) {
        stopRequested = false
        val minBuffer = AudioRecord.getMinBufferSize(RATE, CHANNEL, ENCODING)
        if (minBuffer <= 0) return@withContext null

        // VOICE_RECOGNITION rather than MIC, for the speech-tuned noise
        // suppression. It is *also* the source Android reroutes to a bluetooth
        // headset when one appears, which is why `pin` below nails it back to
        // the built-in mic: the gate calibrated its thresholds on that
        // channel, and a turn recorded through a different one is a turn
        // Whisper hears differently for no reason the user can see.
        val record = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
                        RATE, CHANNEL, ENCODING, minBuffer * 4)
        } catch (e: Exception) {
            return@withContext null
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            return@withContext null
        }
        context?.let { ctx ->
            try {
                val am = ctx.getSystemService(android.content.Context.AUDIO_SERVICE)
                        as android.media.AudioManager
                am.getDevices(android.media.AudioManager.GET_DEVICES_INPUTS)
                    .firstOrNull { it.type == android.media.AudioDeviceInfo.TYPE_BUILTIN_MIC }
                    ?.let { record.setPreferredDevice(it) }
            } catch (e: Exception) { /* default route is still a mic */ }
        }

        val pcm = ByteArrayOutputStream()
        val frame = ShortArray(minBuffer / 2)
        val bars = FloatArray(BARS)
        var spoke = false
        var quietFor = 0
        var elapsed = 0
        // What has not been sent ahead yet, and how quiet it has been. Only
        // touched when a caller actually wants chunks — the handsfree path
        // and the one-shot path pay nothing for this.
        val pending = ByteArrayOutputStream()
        val chunker = Chunker()

        try {
            record.startRecording()
            while (!stopRequested && elapsed < MAX_MS) {
                val n = record.read(frame, 0, frame.size)
                if (n <= 0) continue

                val ms = n * 1000 / RATE
                elapsed += ms

                var sum = 0.0
                for (i in 0 until n) {
                    val v = frame[i].toDouble()
                    sum += v * v
                }
                val rms = sqrt(sum / n)

                // Shift the bar window left and push the newest level on the
                // right, so the waveform reads as time passing rather than as
                // twenty-one independent meters.
                System.arraycopy(bars, 1, bars, 0, BARS - 1)
                bars[BARS - 1] = min(1.0, rms / 6000.0).toFloat()
                _levels.value = bars.copyOf()

                val buf = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN)
                for (i in 0 until n) buf.putShort(frame[i])
                pcm.write(buf.array())

                if (onChunk != null) {
                    pending.write(buf.array())
                    if (chunker.sawFrame(ms, rms)) {
                        onChunk(pending.toByteArray(), false)
                        pending.reset()
                    }
                }

                if (untilSilence) {
                    if (rms > SILENCE_RMS) {
                        spoke = true
                        quietFor = 0
                    } else if (spoke) {
                        quietFor += ms
                        if (quietFor >= SILENCE_MS) break
                    } else if (giveUpMs > 0 && elapsed >= giveUpMs) {
                        // Nobody said anything. This is how a conversation
                        // ends without a goodbye: the turn simply does not
                        // arrive, and the caller closes the session.
                        return@withContext null
                    }
                }
            }
        } catch (e: Exception) {
            return@withContext null
        } finally {
            try { record.stop() } catch (e: Exception) { /* already stopped */ }
            record.release()
            _levels.value = FloatArray(BARS)
        }

        val body = pcm.toByteArray()
        // Under a fifth of a second is a button brushed, not a message. The
        // server treats an empty transcript as an ordinary thing rather than an
        // error, but not sending it at all is quieter still.
        val brushed = body.size < RATE / 5 * 2
        if (onChunk != null && !brushed) {
            // The tail, always, even when it is empty: the final chunk is what
            // ends the utterance and carries the answer back, so a caller that
            // never sees one waits forever. A held tail silently dropped is the
            // bug this line exists to not have.
            onChunk(pending.toByteArray(), true)
        }
        if (brushed) null else wav(body)
    }

    /**
     * When to cut a chunk — the whole rule, with no microphone in it.
     *
     * Split out of the record loop so it can be tested, because the two
     * conditions are not interchangeable and getting either wrong is a
     * regression that only shows up as a slightly wrong transcript:
     *
     *  * **Long enough.** Whisper pads every call to a 30-second window, so
     *    the encoder costs the same for one second of audio as for twenty.
     *    Cutting eagerly does not spread the work out, it multiplies it —
     *    measured, a 3.2s clip in four one-second pieces cost 8.1s of compute
     *    against 1.6s in one pass.
     *  * **At a silence.** A cut mid-word damages the transcript on both
     *    sides. Measured, an arbitrary cut turned "Good evening sir" into
     *    "Whose evening, sir". This one is a correctness condition, not a
     *    tuning knob.
     */
    class Chunker(private val minMs: Int = CHUNK_MIN_MS,
                  private val quietMs: Int = CHUNK_SILENCE_MS) {

        var pendingMs = 0; private set
        private var quietFor = 0

        /** Feed one frame; true means cut here and send what has piled up. */
        fun sawFrame(ms: Int, rms: Double): Boolean {
            pendingMs += ms
            quietFor = if (rms > SILENCE_RMS) 0 else quietFor + ms
            if (pendingMs < minMs || quietFor < quietMs) return false
            pendingMs = 0
            quietFor = 0
            return true
        }
    }

    /** A 44-byte canonical WAV header in front of the samples. */
    private fun wav(pcm: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(44 + pcm.size)
        val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        val byteRate = RATE * 2
        header.put("RIFF".toByteArray())
        header.putInt(36 + pcm.size)
        header.put("WAVE".toByteArray())
        header.put("fmt ".toByteArray())
        header.putInt(16)                  // PCM chunk size
        header.putShort(1)                 // PCM, uncompressed
        header.putShort(1)                 // mono
        header.putInt(RATE)
        header.putInt(byteRate)
        header.putShort(2)                 // block align
        header.putShort(16)                // bits per sample
        header.put("data".toByteArray())
        header.putInt(pcm.size)
        out.write(header.array())
        out.write(pcm)
        return out.toByteArray()
    }
}
