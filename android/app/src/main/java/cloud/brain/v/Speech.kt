package cloud.brain.v

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch

/**
 * V's answer, read aloud a sentence at a time as the laptop makes them.
 *
 * ### The shape this replaces
 *
 * The phone used to get the answer as JSON and then call `POST /v1/speak`,
 * which synthesized *the whole reply* and returned one blob. Measured on
 * Parsa's laptop that was ~4.4 seconds against the hosted voice — a wait that
 * began only after V already knew every word it was going to say, and that no
 * model choice fixes, because it is the wrong shape rather than the wrong
 * speed.
 *
 * Now the server splits the answer into sentences and starts on the first one
 * the moment the turn exists — before this phone has even parsed the JSON —
 * and `GET /v1/say` streams them as they are made. Downloading and playing run
 * at once here, so sentence two is arriving while sentence one is still being
 * spoken. What the ear waits for is the first sentence, not the last.
 *
 * ### Falling back
 *
 * A turn with no [Turn.turn] had nothing spooled — a server too old for it, a
 * voice that failed to load, a turn nobody asked to have spoken. Those go
 * through the original one-blob call, which still works and is still what the
 * Telegram surface does. No voice at all is not a failed turn either: the
 * words are already on screen.
 */
class Speech(private val api: Api, private val player: Player) {

    /**
     * Speak [turn], suspending until it has finished or the caller cancels.
     *
     * Cancellation is the barge-in path and has to be immediate, so the
     * download and the playback are children of one scope: cancelling this
     * cancels both, and `Player.stop` runs from `play`'s own cancellation
     * handler.
     */
    suspend fun play(turn: Turn) {
        val words = turn.spoken.ifBlank { turn.text }
        if (words.isBlank()) return
        if (turn.turn.isBlank()) {
            oneShot(words)
            return
        }
        try {
            coroutineScope {
                // Unlimited, and deliberately: a sentence of Opus is a few KB,
                // there are single digits of them, and the alternative — a
                // rendezvous channel — would stall the download every time it
                // got ahead of the speaking, which is precisely the thing this
                // class exists to let it do.
                val frames = Channel<ByteArray>(Channel.UNLIMITED)
                val fetch = launch {
                    try {
                        api.say(turn.turn) { frames.send(it) }
                    } catch (e: Exception) {
                        // The stream died mid-answer. Whatever already arrived
                        // still gets spoken; the rest is lost, and that is a
                        // better outcome than silence for the whole turn.
                    } finally {
                        frames.close()
                    }
                }
                var spoke = false
                for (frame in frames) {
                    // Barge-in cancels this scope and stops the player, and
                    // the next frame must not start in the gap between those
                    // two. Checked here rather than relied on at `play`'s own
                    // suspension point, because a frame that has already begun
                    // is a sentence spoken over the top of him.
                    kotlinx.coroutines.currentCoroutineContext().ensureActive()
                    spoke = true
                    player.play(frame)
                }
                fetch.join()
                // The spool produced nothing — a voice that failed to load on
                // the laptop. Fall back rather than leaving the turn silent.
                if (!spoke) oneShot(words)
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            oneShot(words)
        }
    }

    private suspend fun oneShot(words: String) {
        try {
            player.play(api.speak(words))
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            // No voice is not a failed turn. The answer is already read.
        }
    }
}
