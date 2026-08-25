package cloud.brain.v

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * The gate's models, on the phone's own CPU.
 *
 * `FeaturesTest` proves the Kotlin front-end reproduces the Python reference on
 * a JVM. That is most of the risk but not all of it: it never loads an ONNX
 * graph, never runs ORT's ARM kernels, and never touches the assets as they are
 * actually packaged. A model that fails to load, an input name that changed
 * between export and runtime, or an arm64 kernel that quietly differs would all
 * pass every desktop test and fail on the only device that matters.
 *
 * So this re-runs **the measurement the whole design rests on** — the
 * speaker-separation margin from the design note, §3 — here, on the phone.
 * Desktop ONNX Runtime scored two speakers at same-min 0.691 against diff-max
 * 0.405, a margin of +0.286. If ARM does not reproduce that, the thresholds
 * derived from it are meaningless.
 *
 * The fixtures are the same two voices, trimmed to 18 seconds each.
 */
@RunWith(AndroidJUnit4::class)
class GatePipelineTest {

    /** The app under test: where the models are packaged. */
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    /** The test APK itself, which is where the voice fixtures live. Two
     *  different asset managers, and reading the wrong one gets you a
     *  `FileNotFoundException` for a file that is plainly there. */
    private val fixtures get() = InstrumentationRegistry.getInstrumentation().context

    private var speaker: SpeakerId? = null
    private var wake: WakeWord? = null
    private var vad: Vad? = null

    @Before fun setUp() = Features.load(context)

    @After fun tearDown() {
        speaker?.close(); wake?.close(); vad?.close()
    }

    /** 16-bit mono PCM out of a WAV in `androidTest/assets`, as ±1 floats. */
    private fun voice(name: String): FloatArray {
        val bytes = fixtures.assets.open(name).use { input ->
            val out = ByteArrayOutputStream()
            input.copyTo(out)
            out.toByteArray()
        }
        // Skip the 44-byte canonical header these were written with.
        val n = (bytes.size - 44) / 2
        return FloatArray(n) { i ->
            val lo = bytes[44 + i * 2].toInt() and 0xFF
            val hi = bytes[44 + i * 2 + 1].toInt()
            ((hi shl 8) or lo).toShort() / 32768f
        }
    }

    // ---- the measurement --------------------------------------------------
    @Test
    fun speakerEmbeddingsSeparateTwoPeopleOnThisPhone() {
        val s = SpeakerId(context).also { speaker = it }
        val segments = listOf("evie.wav", "julian.wav").associateWith { name ->
            val a = voice(name)
            val len = 16_000 * 3
            (0 until 6).map { i -> s.embed(a.copyOfRange(i * len, (i + 1) * len)) }
        }

        val names = segments.keys.toList()
        val same = names.flatMap { n ->
            val e = segments.getValue(n)
            (0 until 6).flatMap { i -> (i + 1 until 6).map { j -> Features.cosine(e[i], e[j]) } }
        }
        val diff = segments.getValue(names[0]).flatMap { a ->
            segments.getValue(names[1]).map { b -> Features.cosine(a, b) }
        }

        val minSame = same.min()
        val maxDiff = diff.max()
        val margin = minSame - maxDiff
        println("on-device: same min=$minSame mean=${same.average()} · " +
                "diff max=$maxDiff mean=${diff.average()} · margin=$margin")

        assertTrue("the distributions overlap on this phone — same-min $minSame " +
                   "is not above diff-max $maxDiff, so no threshold can separate " +
                   "Parsa from anyone else", margin > 0f)
        // Desktop measured +0.286. Allowing half of it still proves the chain,
        // while catching the case where ARM kernels degrade it meaningfully.
        assertTrue("margin $margin collapsed versus the desktop's +0.286",
                   margin > 0.14f)
    }

    @Test
    fun embeddingsAreUnitLengthAndFinite() {
        val s = speaker ?: SpeakerId(context).also { speaker = it }
        val e = s.embed(voice("evie.wav").copyOfRange(0, 16_000 * 2))
        assertEquals(SpeakerId.DIMS, e.size)
        var sum = 0f
        for (x in e) {
            assertTrue("embedding contains a non-finite value", x.isFinite())
            sum += x * x
        }
        assertEquals("embeddings must be L2-normalised for cosine to be cosine",
                     1f, sqrt(sum), 1e-3f)
    }

    // ---- the other two stages --------------------------------------------
    @Test
    fun wakeDescriptorIsStableForTheSameAudio() {
        val w = WakeWord(context).also { wake = it }
        val a = voice("evie.wav").copyOfRange(0, 16_000)
        val int16 = FloatArray(a.size) { a[it] * 32768f }

        val one = w.describe(int16)!!
        val two = w.describe(int16)!!
        assertEquals(WakeWord.DESCRIPTOR, one.size)
        // Determinism is not free on a threaded runtime, and a matcher whose
        // score wobbles between runs cannot have a threshold set against it.
        for (i in one.indices) {
            assertTrue("descriptor is not deterministic at $i",
                       abs(one[i] - two[i]) < 1e-5f)
        }
        assertEquals("a descriptor must be its own best match",
                     1f, w.score(one, listOf(two)), 1e-4f)
    }

    @Test
    fun wakeDescriptorRejectsAudioTooShortToHoldTheWord() {
        val w = wake ?: WakeWord(context).also { wake = it }
        // 100ms. Below the embedding network's 76-frame window, so there is
        // nothing to describe and the gate must skip it rather than throw.
        assertTrue(w.describe(FloatArray(1_600)) == null)
    }

    @Test
    fun vadSeparatesSpeechFromSilence() {
        val v = Vad(context).also { vad = it }
        val speech = voice("julian.wav")

        v.reset()
        var quiet = 0f
        repeat(20) { quiet = maxOf(quiet, v.probability(FloatArray(Vad.WINDOW))) }

        v.reset()
        var loud = 0f
        // Skip the first second: these recordings open on a breath.
        for (k in 32 until 92) {
            loud = maxOf(loud, v.probability(
                speech.copyOfRange(k * Vad.WINDOW, (k + 1) * Vad.WINDOW)))
        }
        println("on-device vad: silence=$quiet speech=$loud")
        assertTrue("silence scored $quiet, which is not silent", quiet < 0.3f)
        assertTrue("speech scored $loud, which is not speech", loud > 0.7f)
    }

    @Test
    fun featuresLoadFromThePackagedAssets() {
        // The JVM test feeds this file from `test/resources`; here it has to
        // come out of the APK, uncompressed, exactly as `noCompress` leaves it.
        val m = Features.logMel(FloatArray(16_000) { i ->
            (0.4 * kotlin.math.sin(2 * Math.PI * 300 * i / 16000.0)).toFloat()
        })
        assertEquals(Features.N_MELS, m.size)
        assertEquals(1 + 16_000 / Features.HOP, m[0].size)
        assertTrue(m.all { row -> row.all { it.isFinite() } })
    }
}
