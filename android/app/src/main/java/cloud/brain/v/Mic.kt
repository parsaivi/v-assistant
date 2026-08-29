package cloud.brain.v

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Who is allowed to hold the microphone.
 *
 * There is exactly one, and until now that was true only by accident: the
 * enrollment screen could only be reached when the gate was not running,
 * because the gate needs a voiceprint and enrollment is what makes one. Adding
 * re-enrollment and a calibration screen breaks that accident — both open an
 * `AudioRecord` while `VService` is already holding one.
 *
 * Two `AudioRecord`s on one microphone is **not** an error Android reports. The
 * second one opens, returns zeroes forever, and the screen shows a voiceprint
 * built from silence. So the arbitration has to be explicit, and it has to be
 * process-wide: the UI has no reference to the `Gate` instance living inside
 * the service, and handing one out would be a lifetime bug waiting to happen.
 *
 * The protocol is two flags and a wait:
 *
 * ```
 * Mic.withUi {          // sets uiWants, waits for the gate to actually let go
 *     …record…          // safe: the gate is parked
 * }                     // clears uiWants, gate resumes on its own
 * ```
 *
 * [gateHolding] is the acknowledgement, and waiting for it is the whole point.
 * [uiWants] alone only raises a flag; the gate's reader is blocked inside
 * `AudioRecord.read` and will not notice for up to one buffer — the same race
 * `VService.onWake` has to wait out before a conversation opens its recorder.
 */
object Mic {

    private val _uiWants = MutableStateFlow(false)

    /** Set by `Gate` while it actually has the microphone open. */
    private val _gateHolding = MutableStateFlow(false)
    val gateHolding: StateFlow<Boolean> = _gateHolding

    /** Read by `Gate` on every loop: false means park and release. */
    val gateMayListen: Boolean get() = !_uiWants.value

    /**
     * Whether a screen currently owns the microphone.
     *
     * Read by `VService.onWake` as a veto. The park flag alone is not enough
     * to stop a wake: the gate cuts an utterance at a silence and scores it
     * *afterwards*, so a segment captured a moment before the button went down
     * can still come back a match while a screen is recording. Without this,
     * holding to talk in chat would be heard twice — once by the composer he
     * is talking into, once by a gate that then greets him and opens a second
     * conversation on top of the one he is having.
     */
    val uiHolding: Boolean get() = _uiWants.value

    internal fun gateHolds(holding: Boolean) { _gateHolding.value = holding }

    /**
     * Run [body] with sole use of the microphone.
     *
     * Waits up to a second and a half for the gate to put it down. If it never
     * does — the service is wedged, or was killed mid-read — [body] runs
     * anyway: a screen that silently refuses to record is worse than one that
     * records badly, and the gate is about to be restarted regardless.
     */
    suspend fun <T> withUi(body: suspend () -> T): T {
        _uiWants.value = true
        try {
            withTimeoutOrNull(1_500) { _gateHolding.first { !it } }
            return body()
        } finally {
            _uiWants.value = false
        }
    }
}
