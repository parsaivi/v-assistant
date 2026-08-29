package cloud.brain.v

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * When a recording is cut into pieces to be transcribed ahead of time.
 *
 * Worth testing on its own because both halves of the rule fail *quietly*.
 * Cutting too eagerly does not break anything visible — it just makes the
 * laptop do several times the work, because Whisper pads every call to a
 * 30-second window and the encoder costs the same either way. Cutting in the
 * wrong place does not break anything visible either — it produces a transcript
 * that is subtly wrong at the seams, which then reaches the vault.
 *
 * The numbers below are the measured ones from `Recorder.CHUNK_MIN_MS`.
 */
class ChunkerTest {

    /** Loud enough to count as speech, and quiet enough to count as a gap.
     *  `Recorder.SILENCE_RMS` is 320. */
    private val speech = 3000.0
    private val quiet = 40.0

    @Test
    fun `a short question is never cut, so it stays one pass`() {
        val c = Recorder.Chunker()
        // Four seconds of talking with ordinary gaps between words. This is
        // the common case and the one that must not regress: cutting here
        // would multiply the compute for an utterance that was already going
        // to be a single fast pass.
        var cuts = 0
        repeat(200) { i ->
            val rms = if (i % 10 < 7) speech else quiet
            if (c.sawFrame(20, rms)) cuts++
        }
        assertEquals("a 4s utterance must not be cut", 0, cuts)
    }

    @Test
    fun `a long dictation is cut, but only at a gap`() {
        val c = Recorder.Chunker()
        // Eight seconds of unbroken speech: over the length threshold, but
        // there is nowhere safe to cut, so it must not.
        repeat(400) { assertFalse(c.sawFrame(20, speech)) }

        // Now he pauses. The cut lands here and nowhere earlier.
        assertFalse("200ms of quiet is not yet a boundary", c.sawFrame(20, quiet))
        var cut = false
        repeat(20) { if (c.sawFrame(20, quiet)) cut = true }
        assertTrue("a real pause after enough audio is where it cuts", cut)
    }

    @Test
    fun `a pause that arrives too early does not cut`() {
        val c = Recorder.Chunker()
        // Two seconds of speech, then a long silence. Under the length
        // threshold, so this is a gap between words and not a chunk boundary —
        // cutting would spend a whole encoder pass on two seconds of audio.
        repeat(100) { c.sawFrame(20, speech) }
        var cuts = 0
        repeat(100) { if (c.sawFrame(20, quiet)) cuts++ }
        assertEquals(0, cuts)
    }

    @Test
    fun `the counter resets after a cut so chunks do not run together`() {
        val c = Recorder.Chunker()
        repeat(400) { c.sawFrame(20, speech) }
        var first = false
        // Stop *at* the cut rather than feeding past it — frames after one are
        // the next chunk, and counting them here is what the reset is for.
        for (i in 0 until 20) {
            if (c.sawFrame(20, quiet)) { first = true; break }
        }
        assertTrue(first)
        assertEquals("the pending length restarts at zero", 0, c.pendingMs)

        // Immediately after a cut, another silence must not cut again — there
        // is nothing behind it to send.
        var again = false
        repeat(30) { if (c.sawFrame(20, quiet)) again = true }
        assertFalse("an empty chunk is never worth a round trip", again)
    }
}
