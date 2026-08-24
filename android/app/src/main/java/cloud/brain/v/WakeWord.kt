package cloud.brain.v

import android.content.Context

/**
 * Did he just say the wake word?
 *
 * **Why this is template matching and not a trained detector.** The obvious
 * build is openWakeWord's own pipeline: generate a few thousand synthetic
 * "Friday"s with Piper, train a small classifier, ship the weights. It is more
 * robust to noise than what is here, and it is what should replace this if the
 * shadow-mode numbers ever justify it. Three things argued against doing it
 * first:
 *
 *  1. It needs a GPU training run before a single line of the app can be
 *     tested end to end, and it has to be redone to change the word.
 *  2. Those models are trained on English TTS. Parsa says "Friday" with a
 *     Persian vowel in it, and a detector trained on American synthetic speech
 *     is worst exactly where it matters most.
 *  3. Enrollment already records fifteen "Friday"s in his own voice, because
 *     the speaker check needs them. Those same recordings are a better
 *     description of the sound he actually makes than synthetic data is, and
 *     they cost nothing extra to use.
 *
 * The features are openWakeWord's, though: Google's `speech_embedding` network
 * (`mel.onnx` → `emb.onnx`), which maps 760ms of audio to 96 dimensions that
 * encode *what was said* rather than who said it. That split is the whole
 * design — this stage answers "was that the word", [SpeakerId] answers "was
 * that him", and neither is asked to do the other's job.
 *
 * Matching is cosine against the enrolled templates, taking the best. Mean and
 * max pooling are concatenated because "Friday" is two syllables with a
 * distinctive stop in the middle: mean pooling alone blurs the sequence into
 * an average that "fry day", "privacy" and "Thursday" all sit close to.
 */
class WakeWord(context: Context) {

    companion object {
        /** The embedding network's fixed input: 76 mel frames of 10ms. */
        private const val FRAMES = 76
        /** Step between windows, in mel frames. 8 = 80ms, openWakeWord's stride. */
        private const val STRIDE = 8
        const val DIMS = 96
        /** mean-pooled ++ max-pooled */
        const val DESCRIPTOR = DIMS * 2

        /** Shortest and longest a "Friday" can plausibly be. Anything outside
         *  is not the word, and skipping it saves the embedding run entirely. */
        const val MIN_SAMPLES = 16_000 * 30 / 100     // 0.30s
        const val MAX_SAMPLES = 16_000 * 160 / 100    // 1.60s
    }

    private val mel = Onnx.session(context, "models/mel.onnx")
    private val emb = Onnx.session(context, "models/emb.onnx")

    /**
     * Reduce a speech segment to a 192-d descriptor, or null if it is too short
     * to hold the word.
     *
     * [pcm] is int16-scaled (not ±1) — openWakeWord's melspectrogram graph was
     * exported expecting raw sample magnitudes, and feeding it normalised audio
     * shifts every mel value by a constant ~26dB.
     */
    fun describe(pcm: FloatArray): FloatArray? {
        if (pcm.size < MIN_SAMPLES) return null

        val melFrames: Array<FloatArray> = Onnx.tensor(pcm, 1, pcm.size.toLong()).use { t ->
            mel.run(mapOf("input" to t)).use { r ->
                @Suppress("UNCHECKED_CAST")
                val o = r[0].value as Array<Array<Array<FloatArray>>>   // [1][1][T][32]
                o[0][0]
            }
        }
        val t = melFrames.size
        if (t < FRAMES) return null

        // openWakeWord's scaling, applied between the two graphs.
        for (row in melFrames) for (i in row.indices) row[i] = row[i] / 10f + 2f

        val starts = (0..(t - FRAMES) step STRIDE).toList()
        val batch = FloatArray(starts.size * FRAMES * 32)
        var o = 0
        for (s in starts) for (f in 0 until FRAMES) {
            System.arraycopy(melFrames[s + f], 0, batch, o, 32); o += 32
        }

        val vecs = Onnx.tensor(batch, starts.size.toLong(), FRAMES.toLong(), 32, 1).use { tt ->
            emb.run(mapOf("input_1" to tt)).use { r ->
                @Suppress("UNCHECKED_CAST")
                val out = r[0].value as Array<Array<Array<FloatArray>>>  // [n][1][1][96]
                Array(starts.size) { i -> out[i][0][0] }
            }
        }

        val mean = FloatArray(DIMS)
        val max = FloatArray(DIMS) { -Float.MAX_VALUE }
        for (v in vecs) for (i in 0 until DIMS) {
            mean[i] += v[i]
            if (v[i] > max[i]) max[i] = v[i]
        }
        for (i in 0 until DIMS) mean[i] /= vecs.size

        val d = FloatArray(DESCRIPTOR)
        System.arraycopy(mean, 0, d, 0, DIMS)
        System.arraycopy(max, 0, d, DIMS, DIMS)
        return Features.l2(d)
    }

    /** Best cosine against any enrolled template, in −1..1. */
    fun score(descriptor: FloatArray, templates: List<FloatArray>): Float {
        var best = -1f
        for (t in templates) {
            val c = Features.cosine(descriptor, t)
            if (c > best) best = c
        }
        return best
    }

    fun close() { mel.close(); emb.close() }
}
