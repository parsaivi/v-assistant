package cloud.brain.v

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext

/**
 * Listening for Parsa talking over V, so a long answer can be cut off.
 *
 * ### The problem this has to solve first
 *
 * Naive barge-in does not work: the microphone hears V's own voice coming out
 * of the phone's speaker, the VAD says "speech", and V interrupts himself
 * within about 200ms of starting to talk — every single time. Acoustic echo is
 * not an edge case here, it is the default outcome.
 *
 * Three things are stacked against it, in order of how much they matter:
 *
 *  1. **`VOICE_COMMUNICATION` as the source.** This is the one input Android
 *     runs its platform echo canceller on; it exists for speakerphone calls,
 *     which is exactly this situation. Note this is a *different* source from
 *     the one [Gate] uses — the gate wants `VOICE_RECOGNITION` and its
 *     speech-tuned noise suppression, and it is never running at the same time
 *     as this, so the two never disagree about the device.
 *  2. **[AcousticEchoCanceler] explicitly**, where the platform offers it as a
 *     separate effect rather than folding it into the source. Availability is
 *     per-device and cannot be assumed.
 *  3. **A sustained-speech requirement.** Even with cancellation, residual echo
 *     produces short VAD blips. Requiring [SUSTAIN_MS] of continuous speech
 *     before calling it an interruption costs a fifth of a second of latency
 *     and removes essentially all of them.
 *
 * ### When it gives up
 *
 * If [AcousticEchoCanceler.isAvailable] is false, the bar is raised rather than
 * the feature disabled — [SUSTAIN_NO_AEC_MS] instead. On a phone with no
 * canceller and a reply playing loudly through the speaker, this may still
 * occasionally self-trigger, and that is an acceptable failure: the cost is a
 * reply cut short, which is one turn lost, whereas *not* having barge-in means
 * every long answer has to be sat through. The user-visible symptom of getting
 * this wrong is bounded and obvious, which is why it ships on rather than off.
 */
class BargeIn(private val context: Context) {

    companion object {
        /** Continuous speech before an interruption is believed, with the
         *  platform canceller doing its job. */
        private const val SUSTAIN_MS = 220

        /** Without one, demand more before believing it. */
        private const val SUSTAIN_NO_AEC_MS = 500
    }

    /**
     * Suspend until Parsa is clearly talking, then return.
     *
     * Cancel this coroutine to stop watching — that is how the caller ends it
     * when playback finished on its own. Returns false if the microphone could
     * not be opened, which is not an error: it just means this turn cannot be
     * interrupted.
     */
    @SuppressLint("MissingPermission")
    suspend fun await(): Boolean = withContext(Dispatchers.Default) {
        val minBuf = AudioRecord.getMinBufferSize(
            Gate.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return@withContext false

        val record = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Gate.RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        maxOf(minBuf * 4, Vad.WINDOW * 16))
        } catch (e: Exception) {
            return@withContext false
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            return@withContext false
        }

        var canceller: AcousticEchoCanceler? = null
        val aec = try {
            if (AcousticEchoCanceler.isAvailable()) {
                canceller = AcousticEchoCanceler.create(record.audioSessionId)
                canceller?.enabled = true
                canceller?.enabled == true
            } else false
        } catch (e: Exception) {
            false
        }
        val needed = if (aec) SUSTAIN_MS else SUSTAIN_NO_AEC_MS

        val vad = Vad(context)
        val frame = ShortArray(Vad.WINDOW)
        val window = FloatArray(Vad.WINDOW)
        var speechMs = 0

        try {
            record.startRecording()
            while (coroutineContext.isActive) {
                var got = 0
                while (got < Vad.WINDOW) {
                    val n = record.read(frame, got, Vad.WINDOW - got)
                    if (n <= 0) return@withContext false
                    got += n
                }
                for (i in 0 until Vad.WINDOW) window[i] = frame[i] / 32768f
                vad.probability(window)

                if (vad.isSpeaking) {
                    speechMs += Vad.WINDOW * 1000 / Gate.RATE
                    if (speechMs >= needed) return@withContext true
                } else {
                    speechMs = 0
                }
            }
            false
        } catch (e: Exception) {
            false
        } finally {
            try { record.stop() } catch (e: Exception) { /* already stopped */ }
            record.release()
            try { canceller?.release() } catch (e: Exception) { /* nothing to free */ }
            vad.close()
        }
    }
}
