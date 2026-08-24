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
                       context: android.content.Context? = null): ByteArray? =
            withContext(Dispatchers.IO) {
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
        if (body.size < RATE / 5 * 2) null else wav(body)
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
