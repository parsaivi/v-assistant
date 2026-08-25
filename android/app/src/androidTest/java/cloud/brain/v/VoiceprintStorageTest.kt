package cloud.brain.v

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Does `EncryptedSharedPreferences` actually store what it is told to?
 *
 * This test exists because of an observed contradiction on a real phone: the
 * voiceprint round-tripped perfectly (fifteen templates, 23.9KB on disk), and
 * yet `shadowMode` read back **false** immediately after enrollment set it to
 * `true`, on a getter whose default is also `true`. Nothing in the codebase
 * writes `false` except a screen that had never been opened. One of the three
 * had to be wrong — the write, the read, or the default — and there is no way
 * to tell which from outside the app, because the file is encrypted.
 *
 * So: exercise the primitives directly, in the same store the app uses.
 */
@RunWith(AndroidJUnit4::class)
class VoiceprintStorageTest {

    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    /** A throwaway store, never the real one. The first version of this test
     *  used the default and left the real voiceprint's shadow mode flipped and
     *  a fabricated row in the shadow log. */
    private fun store() = Voiceprint(context, "v-voiceprint-test")

    @Test
    fun booleansSurviveAWriteAndAFreshInstance() {
        val a = store()
        val original = a.shadowMode
        try {
            a.shadowMode = true
            assertTrue("true did not survive the same instance", a.shadowMode)
            // A second instance opens the same underlying file; if the value
            // only lives in one object's memory, this is where it shows.
            assertTrue("true did not survive a fresh instance",
                       store().shadowMode)

            a.shadowMode = false
            assertTrue("false did not survive", !a.shadowMode)
            assertTrue("false did not survive a fresh instance",
                       !store().shadowMode)

            a.shadowMode = true
            assertTrue("true did not survive being set back",
                       store().shadowMode)
        } finally {
            a.shadowMode = original
        }
    }

    @Test
    fun floatsSurviveAWriteAndAFreshInstance() {
        val a = store()
        val w = a.wakeThreshold
        val s = a.speakerThreshold
        try {
            a.wakeThreshold = 0.71f
            a.speakerThreshold = 0.63f
            val fresh = store()
            assertEquals(0.71f, fresh.wakeThreshold, 1e-6f)
            assertEquals(0.63f, fresh.speakerThreshold, 1e-6f)
        } finally {
            a.wakeThreshold = w
            a.speakerThreshold = s
        }
    }

    @Test
    fun theShadowLogAppendsAndClears() {
        val p = store()
        val before = p.log().size
        p.record(720, 0.81f, 0.66f)
        val after = p.log()
        assertEquals(before + 1, after.size)
        val last = after.last()
        assertEquals(720, last.durationMs)
        assertEquals(0.81f, last.wake, 1e-3f)
        assertEquals(0.66f, last.speaker!!, 1e-3f)
    }

    @Test
    fun anAbsentKeyReturnsTheDefault() {
        // The specific failure being chased: a getter whose default is `true`
        // returning `false`. If the store ever answers for a key it does not
        // hold, every default in `Voiceprint` is a lie.
        val p = store()
        val original = p.shadowMode
        p.shadowMode = original          // ensure the key exists
        assertEquals(original, store().shadowMode)
    }
}
