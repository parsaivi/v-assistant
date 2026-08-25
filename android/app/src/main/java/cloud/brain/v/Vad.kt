package cloud.brain.v

import ai.onnxruntime.OnnxTensor
import android.content.Context
import java.nio.FloatBuffer

/**
 * Silero VAD: is this 32ms of audio speech?
 *
 * The first and cheapest gate, and the one that makes the other two affordable.
 * The wake matcher and the speaker model are far too expensive to run on every
 * frame of a day; this decides which frames are worth looking at, and in a
 * quiet room it rejects essentially all of them for about 0.3ms each.
 *
 * An RMS threshold would be free and was the obvious first move — `Recorder`
 * already computes one for the waveform. It was not enough: a fan, a keyboard
 * and traffic through a window all clear any RMS threshold that a quiet
 * sentence also clears, so the wake matcher would have run continuously in
 * exactly the rooms Parsa actually sits in. Silero distinguishes speech from
 * loud, and that is the distinction that matters here.
 *
 * The model is stateful — `state` carries the RNN across calls — so frames must
 * be fed in order and [reset] called at the start of each listening session.
 */
class Vad(context: Context) {

    companion object {
        /** Silero v5 at 16kHz is fixed at 512-sample windows. Not a tunable. */
        const val WINDOW = 512

        /**
         * Samples of the *previous* window prepended to each call.
         *
         * This is not an optimisation, it is a correctness requirement, and
         * getting it wrong is the most expensive mistake made in this gate so
         * far. Silero v5's ONNX graph expects 576 samples at 16kHz — 64 of
         * context plus the 512 new ones — because its first convolution needs
         * history the exported graph does not keep for itself. The Python
         * wrapper does this concatenation before every call, which is why the
         * requirement is invisible if you read only the model's input shape
         * (`[None, None]`, happily accepting 512).
         *
         * Fed bare 512-sample windows the model does not fail. It returns
         * plausible, well-formed probabilities that are simply always near
         * zero: measured over twenty seconds of clear speech, max 0.154 and not
         * one frame above the 0.5 threshold. The gate was perfectly healthy and
         * permanently deaf. With the context restored, the same audio scores
         * max 1.000 with 90% of frames above threshold.
         */
        const val CONTEXT = 64
        private const val SPEECH = 0.5f
        /** Hysteresis: leaving speech is harder than entering it, so a pause
         *  between two words does not split one utterance into two. */
        private const val SILENCE = 0.35f
    }

    private val session = Onnx.session(context, "models/vad.onnx")
    private var state = FloatArray(2 * 1 * 128)
    private var context64 = FloatArray(CONTEXT)
    private val buffer = FloatArray(CONTEXT + WINDOW)
    private var speaking = false

    fun reset() {
        state = FloatArray(2 * 1 * 128)
        context64 = FloatArray(CONTEXT)
        speaking = false
    }

    /**
     * Feed exactly [WINDOW] samples (±1 scaled). Returns the speech probability
     * and updates [speaking], which applies the hysteresis above.
     */
    fun probability(window: FloatArray): Float {
        require(window.size == WINDOW) { "silero wants $WINDOW samples, got ${window.size}" }
        System.arraycopy(context64, 0, buffer, 0, CONTEXT)
        System.arraycopy(window, 0, buffer, CONTEXT, WINDOW)
        val inputs = mapOf(
            "input" to Onnx.tensor(buffer, 1, (CONTEXT + WINDOW).toLong()),
            "state" to Onnx.tensor(state, 2, 1, 128),
            "sr" to OnnxTensor.createTensor(Onnx.env, longArrayOf(16_000L))
        )
        var p: Float
        session.run(inputs).use { r ->
            @Suppress("UNCHECKED_CAST")
            p = (r[0].value as Array<FloatArray>)[0][0]
            val s = r[1].value
            val flat = FloatArray(state.size)
            var i = 0
            @Suppress("UNCHECKED_CAST")
            (s as Array<Array<FloatArray>>).forEach { a -> a.forEach { b ->
                System.arraycopy(b, 0, flat, i, b.size); i += b.size } }
            state = flat
        }
        inputs.values.forEach { it.close() }
        // Carry the tail of this window into the next call.
        System.arraycopy(window, WINDOW - CONTEXT, context64, 0, CONTEXT)
        speaking = if (speaking) p >= SILENCE else p >= SPEECH
        return p
    }

    val isSpeaking: Boolean get() = speaking

    fun close() = session.close()
}
