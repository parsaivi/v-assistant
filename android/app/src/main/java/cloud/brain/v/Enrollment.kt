package cloud.brain.v

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext
import kotlin.math.abs

/**
 * Teaching the gate what Parsa sounds like saying "Friday".
 *
 * Everything the gate knows comes from here, which makes enrollment the one
 * step that cannot be skipped and the one most worth getting fussy about. Two
 * things it does that a naive version would not:
 *
 * **It records through the same path the gate will listen on.** Same source,
 * same pinned built-in mic, same VAD segmentation, same 16kHz. A voiceprint
 * captured through a different channel than it is verified against is the
 * classic way this feature ships broken — the enrollment sounds fine, the
 * numbers look fine, and the thresholds are calibrated against audio the gate
 * will never see.
 *
 * **It throws samples away.** Of fifteen takes, some will be a cough, a
 * clipped first syllable, or the microwave. Averaging those into the centroid
 * moves it away from him and toward the room, which loosens every future
 * comparison in the direction of accepting strangers. [commit] drops the
 * outliers before averaging, and reports how many it dropped so a bad
 * enrollment is visible rather than merely worse.
 */
class Enrollment(private val context: Context, private val prints: Voiceprint) {

    data class Sample(
        val pcm: ShortArray,
        val descriptor: FloatArray,
        val embedding: FloatArray,
        val durationMs: Int,
    )

    data class Result(val kept: Int, val dropped: Int, val cohesion: Float)

    /** One live test: what the gate would have scored for this utterance. */
    data class Probe(
        val durationMs: Int,
        val wake: Float,
        val speaker: Float?,
        val tooShort: Boolean,
    )

    private val samples = mutableListOf<Sample>()

    val count: Int get() = samples.size

    fun reset() = samples.clear()

    /**
     * Wait for one utterance and keep it, or return null if what arrived was
     * not plausibly the word — too short, too long, or unusable by the
     * embedding network. The caller just asks again.
     */
    suspend fun capture(wake: WakeWord, speaker: SpeakerId): Sample? {
        val pcm = Mic.withUi { recordOneUtterance() } ?: return null
        if (pcm.size < WakeWord.MIN_SAMPLES || pcm.size > WakeWord.MAX_SAMPLES) return null

        val int16 = FloatArray(pcm.size) { pcm[it].toFloat() }
        val descriptor = wake.describe(int16) ?: return null

        // The speaker model wants a second; a lone "Friday" is shorter than
        // that, so pad with silence rather than refuse. Padding is honest here
        // because the gate does the same thing at match time.
        val padded = FloatArray(maxOf(pcm.size, SpeakerId.MIN_SAMPLES))
        for (i in pcm.indices) padded[i] = pcm[i] / 32768f
        val embedding = speaker.embed(padded)

        return Sample(pcm, descriptor, embedding, pcm.size * 1000 / Gate.RATE)
            .also { samples.add(it) }
    }

    /**
     * Average what survives outlier rejection into a voiceprint.
     *
     * Outliers are found against the median pairwise cosine rather than the
     * mean, because the mean is exactly what a bad sample drags — using it to
     * detect bad samples lets one cough hide itself.
     */
    fun commit(): Result {
        require(samples.size >= 3) { "need at least three takes to tell a bad one" }

        val embeddings = samples.map { it.embedding }
        val scores = embeddings.map { e ->
            embeddings.filter { it !== e }
                .map { Features.cosine(e, it) }
                .average().toFloat()
        }
        val kept = keepers(scores)

        prints.enroll(
            wakeTemplates = kept.map { samples[it].descriptor },
            speakerEmbeddings = kept.map { samples[it].embedding })

        return Result(kept.size, samples.size - kept.size,
                      scores.sorted()[scores.size / 2])
    }

    companion object {
        /**
         * Which takes survive outlier rejection, given each one's mean cosine
         * to the others.
         *
         * Pulled out as a pure function so it can be tested without a
         * microphone, because the failure it guards against is silent: a cough
         * averaged into the centroid moves it toward the room and away from
         * Parsa, which loosens every future comparison in the direction of
         * accepting strangers. Nothing about that is visible at enrollment
         * time.
         *
         * Median and MAD rather than mean and standard deviation, because the
         * mean is exactly what a bad sample drags — using it to detect bad
         * samples lets one cough hide itself. The `- 0.02` slack keeps a tight,
         * consistent enrollment (where MAD is near zero) from rejecting takes
         * for varying by a thousandth.
         *
         * Never returns fewer than three: below that the centroid is worse than
         * one built from everything, outliers included.
         */
        fun keepers(scores: List<Float>): List<Int> {
            if (scores.size < 3) return scores.indices.toList()
            val median = scores.sorted()[scores.size / 2]
            val mad = scores.map { abs(it - median) }.sorted()[scores.size / 2]
            val floor = median - 3f * mad - 0.02f
            val keep = scores.indices.filter { scores[it] >= floor }
            return if (keep.size >= 3) keep else scores.indices.toList()
        }
    }

    /**
     * Score one utterance the way the gate would, without acting on it.
     *
     * This is what makes calibration a measurement rather than archaeology on a
     * log. Reading a week of scores tells you the shape of the distributions
     * but not which entry was which; saying "Friday" and being handed the two
     * numbers it produced closes the loop in one second.
     *
     * It deliberately mirrors `Gate.evaluate` — same head-trimming, same
     * padding to [SpeakerId.MIN_SAMPLES] — because a probe that measured
     * something slightly different from what the gate measures would be worse
     * than no probe at all.
     */
    suspend fun probe(wake: WakeWord, speaker: SpeakerId): Probe? {
        val pcm = Mic.withUi { recordOneUtterance() } ?: return null
        if (pcm.size < WakeWord.MIN_SAMPLES) {
            return Probe(pcm.size * 1000 / Gate.RATE, 0f, null, tooShort = true)
        }
        val head = if (pcm.size > WakeWord.MAX_SAMPLES)
            pcm.copyOfRange(0, WakeWord.MAX_SAMPLES) else pcm

        val int16 = FloatArray(head.size) { head[it].toFloat() }
        val descriptor = wake.describe(int16)
            ?: return Probe(head.size * 1000 / Gate.RATE, 0f, null, tooShort = true)
        val wakeScore = wake.score(descriptor, prints.templates())

        val padded = FloatArray(maxOf(head.size, SpeakerId.MIN_SAMPLES))
        for (i in head.indices) padded[i] = head[i] / 32768f
        val speakerScore = prints.centroid()?.let {
            Features.cosine(speaker.embed(padded), it)
        }
        return Probe(head.size * 1000 / Gate.RATE, wakeScore, speakerScore, false)
    }

    // ---- capture ----------------------------------------------------------
    @SuppressLint("MissingPermission")
    private suspend fun recordOneUtterance(): ShortArray? = withContext(Dispatchers.Default) {
        Features.load(context)
        val minBuf = AudioRecord.getMinBufferSize(
            Gate.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return@withContext null

        val record = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, Gate.RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        maxOf(minBuf * 4, Vad.WINDOW * 16))
        } catch (e: Exception) {
            return@withContext null
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) { record.release(); return@withContext null }
        try {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am.getDevices(AudioManager.GET_DEVICES_INPUTS)
                .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
                ?.let { record.setPreferredDevice(it) }
        } catch (e: Exception) { /* default route is still a mic */ }

        val vad = Vad(context)
        val frame = ShortArray(Vad.WINDOW)
        val window = FloatArray(Vad.WINDOW)
        val seg = ArrayList<Short>(Gate.RATE)
        var inSpeech = false
        var quietMs = 0
        var waitedMs = 0

        try {
            record.startRecording()
            while (coroutineContext.isActive) {
                var got = 0
                while (got < Vad.WINDOW) {
                    val n = record.read(frame, got, Vad.WINDOW - got)
                    if (n <= 0) return@withContext null
                    got += n
                }
                for (i in 0 until Vad.WINDOW) window[i] = frame[i] / 32768f
                vad.probability(window)
                val ms = Vad.WINDOW * 1000 / Gate.RATE

                if (vad.isSpeaking) {
                    inSpeech = true; quietMs = 0
                    for (x in frame) seg.add(x)
                } else if (inSpeech) {
                    for (x in frame) seg.add(x)
                    quietMs += ms
                    if (quietMs >= 320) return@withContext seg.toShortArray()
                } else {
                    waitedMs += ms
                    if (waitedMs > 10_000) return@withContext null   // he walked away
                }
                if (seg.size > Gate.RATE * 4) return@withContext seg.toShortArray()
            }
            null
        } catch (e: Exception) {
            null
        } finally {
            try { record.stop() } catch (e: Exception) { /* already stopped */ }
            record.release()
            vad.close()
        }
    }
}
