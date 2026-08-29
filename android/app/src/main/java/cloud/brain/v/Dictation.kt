package cloud.brain.v

import android.content.Context
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope

/**
 * Recording and uploading at the same time, without either waiting for the
 * other.
 *
 * ### Why this is not just a callback
 *
 * The obvious shape — hand `Recorder` a callback that posts each chunk — has a
 * bug that only shows up as a slightly wrong transcript. `Recorder`'s loop is
 * a tight `AudioRecord.read`, and its buffer holds on the order of a hundred
 * milliseconds. Awaiting an HTTP POST inside that loop stops draining the
 * buffer for as long as the round trip takes, and what overflows is *dropped
 * audio* — silently, with no error anywhere, showing up later as a missing
 * word at a chunk boundary. Which would be a rich irony for a mechanism whose
 * whole justification is that chunk boundaries must be clean.
 *
 * So the callback does nothing but hand the bytes to a channel, and a separate
 * coroutine drains it. The microphone loop never blocks on the network, and
 * the uploads still happen strictly in order because a channel with one
 * consumer is a queue — which matters, since `stt.Stream` on the other end
 * appends each piece to a transcript and has no way to reorder them.
 *
 * Shared by both surfaces because they need exactly the same thing and had
 * started to grow two copies of it.
 */
class Dictation(private val api: Api, private val recorder: Recorder) {

    /**
     * [turn] is set when the streamed path carried the answer all the way.
     * [wav] is the whole recording, kept so a caller can fall back to the
     * one-shot endpoint when [turn] is null — which happens when the chunked
     * route broke mid-sentence, and must not cost him the thing he just said.
     *
     * Both null means nothing was said, or the microphone could not be opened.
     */
    data class Heard(val turn: Turn?, val wav: ByteArray?)

    suspend fun listen(
        untilSilence: Boolean,
        giveUpMs: Int = 0,
        context: Context? = null,
        speak: Boolean = true,
        onPartial: (String) -> Unit = {},
        onThinking: () -> Unit = {},
    ): Heard = coroutineScope {
        val utterance = java.util.UUID.randomUUID().toString()
            .replace("-", "").take(16)
        // Unlimited: a chunk is at most a few hundred KB and there are single
        // digits of them. A rendezvous channel would put the stall back.
        val queue = Channel<Pair<ByteArray, Boolean>>(Channel.UNLIMITED)

        val uploads = async {
            var answer: Turn? = null
            var broken = false
            for ((pcm, final) in queue) {
                if (broken) continue            // drain, so the channel closes
                try {
                    val o = api.voiceChunk(utterance, pcm, final, speak = speak)
                    if (final) answer = Turn.parse(o)
                    else onPartial(o.optString("transcript"))
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    // The chunked route is an optimisation. If it breaks, the
                    // whole recording is still in hand and the caller falls
                    // back — but the recording itself must not be disturbed.
                    broken = true
                }
            }
            if (broken) null else answer
        }

        val wav = recorder.record(
            untilSilence = untilSilence, giveUpMs = giveUpMs, context = context,
            onChunk = { pcm, final ->
                if (final) onThinking()
                // `trySend`, never `send`: this runs on the microphone's own
                // loop and the one thing it must not do is suspend. An
                // unlimited channel cannot refuse, so nothing is lost.
                queue.trySend(pcm to final)
                if (final) queue.close()
            })

        // A recording too short to return never emitted a final chunk, so the
        // channel is still open and the uploader would wait on it forever.
        queue.close()
        Heard(uploads.await(), wav)
    }
}
