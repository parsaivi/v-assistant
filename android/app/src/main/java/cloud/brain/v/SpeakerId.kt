package cloud.brain.v

import ai.onnxruntime.OnnxTensor
import android.content.Context

/**
 * Was that Parsa?
 *
 * TitaNet-small (NeMo), 192-dimensional embeddings, compared by cosine against
 * the centroid built at enrollment.
 *
 * **Why this model.** WeSpeaker's CAM++ was tried first and rejected on
 * measurement, not taste. On a male/female pair it scored same-speaker 0.92 and
 * different-speaker 0.86 — the two distributions overlapped, so no threshold
 * existed that would admit Parsa and refuse anyone else. The cause was visible
 * in the embeddings: 95% of each vector's magnitude was a constant offset
 * shared by every utterance, so cosine was mostly measuring that offset. It is
 * fixable with a global mean subtracted before scoring, but that mean has to
 * come from a corpus the phone does not have. TitaNet needs no such correction
 * and separated the same pair by a margin of 0.29 with nothing overlapping.
 *
 * **The known weak spot**, stated here because it is where this will fail
 * first: the wake segment is short. "Friday" is around 700ms, and speaker
 * embeddings get better with more speech — the 0.29 margin above was measured
 * on 3-second segments. [MIN_SAMPLES] pads the segment from the rolling buffer
 * to claw some of that back, and the shadow-mode log is what will say whether
 * it was enough.
 */
class SpeakerId(context: Context) {

    companion object {
        const val DIMS = 192
        /** Below a second of audio the embedding gets noticeably noisier, so
         *  the gate pads short wake segments with surrounding buffer. */
        const val MIN_SAMPLES = 16_000
    }

    private val session = Onnx.session(context, "models/speaker.onnx")

    /** [pcm] is ±1 scaled — NeMo's featurizer, unlike openWakeWord's, was
     *  trained on normalised audio. */
    fun embed(pcm: FloatArray): FloatArray {
        val feats = Features.logMel(pcm)                 // [80][frames]
        val frames = feats[0].size
        val flat = Onnx.flatten(feats)
        val signal = Onnx.tensor(flat, 1, Features.N_MELS.toLong(), frames.toLong())
        val length = OnnxTensor.createTensor(Onnx.env, longArrayOf(frames.toLong()))
        try {
            session.run(mapOf("audio_signal" to signal, "length" to length)).use { r ->
                @Suppress("UNCHECKED_CAST")
                val e = (r.get("embs").get().value as Array<FloatArray>)[0]
                return Features.l2(e.copyOf())
            }
        } finally {
            signal.close(); length.close()
        }
    }

    fun close() = session.close()
}
