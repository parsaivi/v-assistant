package cloud.brain.v

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The design tokens, transcribed from
 * `assistant/Voice interface design brief(1)/design_handoff_v_assistant/README.md`.
 *
 * Every hex here is quoted from that document rather than picked, because the
 * brief says so in as many words: "Colors, typography, spacing, motion timings
 * and copy are final and should be reproduced exactly."
 *
 * The terminal surface (`core/ui.py`) holds the same palette as ANSI escapes
 * and made two deviations a terminal forced on it — body text left at the
 * terminal default, and state-card headers not letter-spaced. Neither applies
 * here: this surface controls its own pixels, so it is the first one that gets
 * to be the design as drawn.
 */
object V {

    // ---- colour ---------------------------------------------------------
    val seed = Color(0xFF7C5CFF)        // V's voice, active state, ✦, focus
    val deep = Color(0xFF5B3FD6)        // outer aperture ring, secondary bars
    val lift = Color(0xFFA793FF)        // core-dot centre, emphasis
    val coreHi = Color(0xFFD8CEFF)      // specular highlight, core dot only

    val void = Color(0xFF08080A)        // app background
    val surface = Color(0xFF0A0A0E)     // body, state cards
    val panel = Color(0xFF0E0E12)       // card background
    val panel2 = Color(0xFF101016)      // nested card / chrome

    val line = Color(0xFF1B1B23)
    val line2 = Color(0xFF1E1E28)
    val line3 = Color(0xFF23232F)
    val hoverLine = Color(0xFF33334A)

    val dim3 = Color(0xFF3E3E4C)        // least-important mono text
    val dim2 = Color(0xFF4E4E5E)        // cost line, metadata
    val dim1 = Color(0xFF5A5A6B)        // section labels
    val muted = Color(0xFF6E6E80)       // secondary body copy
    val body = Color(0xFF8A8A9A)        // body copy
    val bodyHi = Color(0xFF9E9EAE)      // emphasised body copy
    val paper = Color(0xFFD6D6E2)       // V's spoken text
    val paperHi = Color(0xFFEDEDF2)     // primary text, the user's own input

    val unsure = Color(0xFFE8A33D)      // low confidence, held writes
    val refuse = Color(0xFFFF6B6B)      // refusal. Never used for "error".
    val free = Color(0xFF5CD9A6)        // local / zero-cost / committed
    val grey = Color(0xFF3B3B49)        // degraded ✦

    /**
     * The four colours V's voice can take, and the only four.
     *
     * The state is decided on the server (`httpapi.voice_state`) and named in
     * the response, so this map is a lookup and never a judgement — a second
     * implementation of the rule here is a second implementation that drifts.
     */
    fun voice(state: String): Color = when (state) {
        "unsure" -> unsure
        "refused" -> refuse
        "degraded" -> grey
        else -> seed
    }

    /** Confidence colouring for the cost line: ≥0.8 green, below that amber. */
    fun confidence(value: Double?): Color = when {
        value == null -> dim2
        value >= 0.8 -> free
        else -> unsure
    }

    // ---- glyphs ---------------------------------------------------------
    // Single mono glyphs. No icon font, no SVG icon library — the brief is
    // explicit that the icon set is six Unicode characters.
    const val VOICE = "✦"       // everything V says. Never a logo.
    const val ROUTE = "◈"       // route / model
    const val NOTE = "⟡"        // memory / vault note
    const val CONF = "◉"        // confidence score
    const val LOCAL = "●"       // local, nothing left the machine
    const val REFUSED = "▲"     // refused

    // ---- type -----------------------------------------------------------
    val sans = FontFamily(
        Font(R.font.plex_sans_300, FontWeight.Light),
        Font(R.font.plex_sans_400, FontWeight.Normal),
        Font(R.font.plex_sans_500, FontWeight.Medium),
        Font(R.font.plex_sans_600, FontWeight.SemiBold),
    )

    /** The terminal, every cost line, every route line, all metadata, all
     *  numbers — and anything V says in text. */
    val mono = FontFamily(
        Font(R.font.plex_mono_300, FontWeight.Light),
        Font(R.font.plex_mono_400, FontWeight.Normal),
        Font(R.font.plex_mono_500, FontWeight.Medium),
        Font(R.font.plex_mono_600, FontWeight.SemiBold),
    )

    /** Persian content only. Same metrics family, so mixed blocks share a
     *  baseline — which is why the Persian block can sit inside the
     *  transcript without shifting the rhythm of the lines around it. */
    val arabic = FontFamily(
        Font(R.font.plex_arabic_300, FontWeight.Light),
        Font(R.font.plex_arabic_400, FontWeight.Normal),
        Font(R.font.plex_arabic_500, FontWeight.Medium),
        Font(R.font.plex_arabic_600, FontWeight.SemiBold),
    )

    // ---- scale ----------------------------------------------------------
    // The brief's type scale, only the sizes this surface actually uses.
    val label = 9.5.sp          // section labels, .2em tracking, uppercase
    val meta = 10.sp
    val cost = 11.sp            // cost lines
    val small = 11.5.sp
    val transcript = 13.sp      // one step up from the terminal's 12.5 —
    val bodyText = 14.sp        // a phone is held further from the eye
    val persian = 15.sp
    val title = 17.sp

    // ---- spacing / radius ------------------------------------------------
    val gutter = 16.dp
    val hang = 22.dp            // cost line indent, aligning under the answer
    val cardRadius = 16.dp
    val blockRadius = 8.dp
    val chipRadius = 4.dp

    // ---- motion ----------------------------------------------------------
    // Two curves only, and nothing in between the three durations. Stated as
    // constants so a call site cannot quietly invent a fourth.
    const val STATE_MS = 120    // a state change
    const val ARRIVE_MS = 320   // something arriving
    const val LOOP_MS = 900     // anything that loops
}
