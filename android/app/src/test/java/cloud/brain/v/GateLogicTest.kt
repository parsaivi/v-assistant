package cloud.brain.v

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two pieces of gate logic that are pure enough to check on the JVM, and
 * wrong-able enough to be worth checking.
 *
 * Everything else in the gate needs a microphone or an ONNX session. These two
 * do not, and both fail quietly rather than loudly: a bad outlier rule produces
 * a slightly wrong voiceprint that still works most of the time, and a bad
 * closer rule ends conversations mid-sentence or never ends them at all.
 */
class GateLogicTest {

    // ---- outlier rejection ------------------------------------------------
    @Test
    fun `a consistent enrollment keeps every take`() {
        // Fifteen takes that agree. Rejecting any of them would mean the rule
        // is chasing noise, and the MAD slack exists precisely to stop that.
        val scores = List(15) { 0.82f + (it % 3) * 0.005f }
        assertEquals(15, Enrollment.keepers(scores).size)
    }

    @Test
    fun `one cough is dropped`() {
        val scores = (List(14) { 0.80f + (it % 4) * 0.004f } + 0.21f)
        val kept = Enrollment.keepers(scores)
        assertFalse("the outlier should not survive", kept.contains(14))
        assertEquals(14, kept.size)
    }

    @Test
    fun `the median is used, so two bad takes cannot hide each other`() {
        // With a mean-based rule these two drag the centre down far enough to
        // sit inside the accepted band — which is the whole reason the rule is
        // written against the median.
        val scores = (List(13) { 0.84f } + 0.30f + 0.28f)
        val kept = Enrollment.keepers(scores)
        assertFalse(kept.contains(13))
        assertFalse(kept.contains(14))
        assertEquals(13, kept.size)
    }

    @Test
    fun `it never returns fewer than three`() {
        // A shambolic enrollment where nothing agrees. A centroid from three
        // bad takes beats a centroid from one.
        val scores = listOf(0.9f, 0.1f, 0.15f, 0.12f)
        assertTrue(Enrollment.keepers(scores).size >= 3)
        assertEquals(2, Enrollment.keepers(listOf(0.9f, 0.1f)).size)
    }

    // ---- closing a conversation -------------------------------------------
    @Test
    fun `spoken closers end the session`() {
        for (phrase in listOf(
            "that's all", "That's all.", "thanks Friday", "goodbye",
            "okay that's all", "we're done", "Nothing else!",
        )) {
            assertTrue("$phrase should close", VoiceSession.closes(phrase))
        }
    }

    @Test
    fun `a bare thanks does not end the session`() {
        // The failure this guards: "thanks" is something Parsa says in the
        // middle of a conversation, and closing on it would make the assistant
        // hang up on him for being polite.
        for (phrase in listOf(
            "thanks", "thank you", "thanks, what about tomorrow",
            "that's all right", "is that all of them",
        )) {
            assertFalse("$phrase should not close", VoiceSession.closes(phrase))
        }
    }

    @Test
    fun `an empty transcript does not close`() {
        assertFalse(VoiceSession.closes(""))
        assertFalse(VoiceSession.closes("   "))
    }
}
