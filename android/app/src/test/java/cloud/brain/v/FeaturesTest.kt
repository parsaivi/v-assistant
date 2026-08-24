package cloud.brain.v

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.sin

/**
 * Does the Kotlin front-end produce the same numbers as the reference?
 *
 * This test exists because of the specific way this code fails. A wrong
 * filterbank, an off-by-one in the reflect padding, or a hann window that is
 * symmetric where librosa's is periodic all produce features that look
 * completely reasonable — finite, smoothly varying, correctly shaped — and
 * feed the speaker model something it has never seen. The model then returns
 * 192 confident floats that encode nothing, no exception is thrown, and the
 * only symptom is that V stops recognising Parsa.
 *
 * `golden_logmel.f32` is the output of the Python pipeline that was measured
 * to separate two speakers by a cosine margin of 0.29. Matching it is what
 * entitles the Kotlin to that same number.
 *
 * The input is generated rather than recorded so both sides compute it
 * identically from the same three sinusoids, with no audio file to drift.
 */
class FeaturesTest {

    private fun signal(n: Int): FloatArray = FloatArray(n) { i ->
        (0.5 * sin(2 * Math.PI * 220 * i / 16000.0) +
         0.3 * sin(2 * Math.PI * 1300 * i / 16000.0 + 0.7) +
         0.1 * sin(2 * Math.PI * 3000 * i / 16000.0)).toFloat()
    }

    private fun resource(name: String) =
        javaClass.classLoader!!.getResourceAsStream(name)!!

    private fun loadBank() = Features.load(resource("mel80.f32"))

    @Test
    fun `log mel matches the python reference`() {
        loadBank()
        val out = Features.logMel(signal(16_000))

        val buf = ByteBuffer.wrap(resource("golden_logmel.f32").readBytes())
            .order(ByteOrder.LITTLE_ENDIAN)
        val mels = buf.int
        val frames = buf.int
        val golden = buf.asFloatBuffer()

        assertEquals("mel bins", mels, out.size)
        assertEquals("frame count", frames, out[0].size)

        var worst = 0f
        var worstAt = ""
        for (m in 0 until mels) for (t in 0 until frames) {
            val d = abs(out[m][t] - golden.get(m * frames + t))
            if (d > worst) { worst = d; worstAt = "mel=$m frame=$t" }
        }
        // Float32 STFT accumulated over 512 taps: agreement to ~1e-3 on
        // normalised features means the same pipeline, not a coincidence.
        assertTrue("max deviation $worst at $worstAt exceeds 2e-3", worst < 2e-3f)
    }

    @Test
    fun `frame count follows the hop`() {
        loadBank()
        // center=True means 1 + samples/hop frames, and the speaker model's
        // `length` input is derived from this — a mismatch there is a crash,
        // which is the one failure mode in this file that is not silent.
        for (secs in listOf(1, 2, 3)) {
            val n = 16_000 * secs
            assertEquals(1 + n / Features.HOP, Features.logMel(signal(n))[0].size)
        }
    }

    @Test
    fun `per feature normalisation leaves each mel bin centred`() {
        loadBank()
        val out = Features.logMel(signal(16_000))
        for (row in out) {
            val mean = row.sum() / row.size
            assertTrue("mel bin mean $mean should be ~0", abs(mean) < 1e-3)
        }
    }

    @Test
    fun `cosine of a vector with itself is one`() {
        val v = Features.l2(floatArrayOf(3f, -4f, 12f, 0.5f))
        assertEquals(1f, Features.cosine(v, v), 1e-5f)
    }
}
