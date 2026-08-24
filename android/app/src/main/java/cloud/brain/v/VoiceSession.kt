package cloud.brain.v

import android.content.Context
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.selects.select

/**
 * A conversation, once the gate has decided there is one.
 *
 * Process-wide state rather than a field on the service, because two things
 * need it and they have different lifetimes: `VService` drives the turns and
 * outlives the UI, while `VoiceScreen` renders them and may not exist yet when
 * the first word is spoken. An object both can see is the smallest thing that
 * survives the activity being created halfway through a sentence.
 *
 * ### The shape of a turn
 *
 * Wake → "Yes?" → listen → answer → listen → … until either nobody says
 * anything for [SILENCE_TIMEOUT_MS] or he says one of [CLOSERS].
 *
 * **No wake word between turns.** That is the whole difference between this
 * and a voice assistant: having to say "Friday" before every sentence is what
 * makes those feel like vending machines. The cost is that the microphone
 * stays open for eight seconds after V finishes talking, which is exactly why
 * the session is visible on screen the entire time it is open — see
 * `VoiceScreen`. An open mic the user cannot see is the thing this design is
 * most careful never to build.
 *
 * **The closers are matched here, not by a model.** They are compared against
 * the transcript Whisper already returned, so ending a conversation costs
 * nothing and cannot fail differently than the transcript did.
 */
object VoiceSession {

    /**
     * Who the wake word summons.
     *
     * The gate is trained on "Friday", and Friday is an agent in `persona.py`
     * with her own memory scope — not a nickname for V. Sending it means the
     * conversation that follows is hers, which is the whole reason the wake
     * word is a name.
     */
    private const val WAKE_AGENT = "friday"

    /** How long the mic stays open with nobody speaking before V gives up. */
    private const val SILENCE_TIMEOUT_MS = 8_000

    /** Said at the end of a turn, these close the session. Deliberately short
     *  and deliberately not fuzzy — "thanks" alone is something Parsa says
     *  mid-conversation, so it is not on the list. */
    private val CLOSERS = listOf(
        "that's all", "thats all", "that is all", "that'll do", "thatll do",
        "thanks friday", "thank you friday", "goodbye", "bye friday",
        "nothing else", "we're done", "were done", "stop listening",
    )

    /** Stripped from the end before matching, so gratitude does not have to be
     *  enumerated into every closing phrase. */
    private val POLITE = listOf("please", "thanks", "thank you", "thanks a lot")

    enum class Phase { IDLE, WAKE, LISTENING, THINKING, SPEAKING }

    data class Line(val fromV: Boolean, val text: String)

    private val _phase = MutableStateFlow(Phase.IDLE)
    val phase: StateFlow<Phase> = _phase

    private val _caption = MutableStateFlow("")
    val caption: StateFlow<String> = _caption

    private val _cost = MutableStateFlow("")
    val cost: StateFlow<String> = _cost

    private val _lines = MutableStateFlow<List<Line>>(emptyList())
    val lines: StateFlow<List<Line>> = _lines

    val isOpen: Boolean get() = _phase.value != Phase.IDLE

    @Volatile private var cancelled = false

    /** Ends the session at the next safe point — the on-screen "done" button. */
    fun close() { cancelled = true }

    /**
     * Run one conversation to its end. Returns when the session closes; the
     * caller is responsible for handing the microphone back to the gate.
     */
    suspend fun run(context: Context, api: Api, player: Player, recorder: Recorder) {
        cancelled = false
        val barge = BargeIn(context)
        _lines.value = emptyList()
        _phase.value = Phase.WAKE
        _caption.value = "Yes?"
        _cost.value = "wake matched locally · \$0.0000"

        try {
            player.takeFocus()

            // The greeting runs no model on the server and says one line, so
            // plugging into a conversation does not begin with a recital.
            val greeting = try { api.wake("gate", WAKE_AGENT) } catch (e: Exception) { null }
            val opening = greeting?.spoken?.ifBlank { "Yes?" } ?: "Yes?"
            _caption.value = opening
            say(api, player, barge, opening)

            while (!cancelled) {
                _phase.value = Phase.LISTENING
                _caption.value = ""
                _cost.value = "listening · nothing sent yet"

                val wav = recorder.record(untilSilence = true,
                                          giveUpMs = SILENCE_TIMEOUT_MS,
                                          context = context)
                if (wav == null || cancelled) break     // silence, or the button

                _phase.value = Phase.THINKING
                _cost.value = "◈ thinking"
                val turn = api.voice(wav)

                val heard = turn.transcript.orEmpty().trim()
                if (heard.isNotEmpty()) _lines.value = _lines.value + Line(false, heard)

                val reply = turn.spoken.ifBlank { turn.text }
                if (reply.isNotBlank()) {
                    _lines.value = _lines.value + Line(true, reply)
                    _phase.value = Phase.SPEAKING
                    _caption.value = reply
                    _cost.value = turn.costLine()
                    say(api, player, barge, reply)
                }

                if (closes(heard)) break
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _caption.value = "Lost him."
            _cost.value = e.message ?: "connection failed"
        } finally {
            player.stop()
            player.releaseFocus()
            _phase.value = Phase.IDLE
            _caption.value = ""
            _cost.value = ""
            // A fresh session next time, so the next wake does not inherit
            // this conversation's context.
            try { api.newSession() } catch (e: Exception) { /* best effort */ }
        }
    }

    /**
     * Speak one reply, and stop the moment Parsa starts talking over it.
     *
     * Playback and the interruption watcher race; whichever finishes first ends
     * the turn and cancels the other. Barge-in is the difference between a
     * conversation and a recital — without it a four-sentence answer has to be
     * sat through even when the first sentence was already wrong.
     *
     * A failed `speak` is not a failed turn. The words are already on screen,
     * which is the same posture the Telegram surface takes when Piper is
     * missing at the other end.
     */
    private suspend fun say(api: Api, player: Player, barge: BargeIn, text: String) {
        val ogg = try {
            api.speak(text)
        } catch (e: Exception) {
            return
        }
        try {
            coroutineScope {
                val playing = async { player.play(ogg) }
                val interrupted = async { barge.await() }
                select<Unit> {
                    playing.onAwait {
                        // Finished on its own; stop listening for a barge-in.
                        interrupted.cancel()
                    }
                    interrupted.onAwait { cutIn ->
                        if (cutIn) player.stop()
                        // `player.play` returns as soon as the MediaPlayer is
                        // stopped, so awaiting it here cannot hang.
                        playing.cancel()
                    }
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            player.stop()
        }
    }

    /**
     * Does this transcript end the conversation?
     *
     * Internal rather than private so the matching can be tested without
     * standing up a conversation — which is how the bug below was found.
     *
     * **An earlier version also matched closers at the *start* of a
     * transcript**, so that "goodbye, I'll be back later" would close. It also
     * closed on "that's all right, what about Tuesday" — because that starts
     * with "that's all " — which is V hanging up mid-question. Prefix matching
     * is unsalvageable in a language where the sentence keeps going, so it is
     * gone. The asymmetry justifies it: failing to close costs eight seconds of
     * silence before the session times out on its own, while closing wrongly
     * loses the thing he was in the middle of asking.
     *
     * Trailing politeness is stripped first, so "that's all, thanks" closes
     * while a bare "thanks" — which he says mid-conversation — does not.
     */
    internal fun closes(transcript: String): Boolean {
        var t = tidy(transcript)
        while (true) {
            if (t.isEmpty()) return false
            if (t in POLITE) return false          // politeness alone is not an ending
            val suffix = POLITE.firstOrNull { t.endsWith(" $it") } ?: break
            t = tidy(t.dropLast(suffix.length + 1))
        }
        return CLOSERS.any { t == it || t.endsWith(" $it") }
    }

    private fun tidy(s: String) =
        s.lowercase().trim().trimEnd('.', '!', '?', ',', '،', '。').trim()
}
