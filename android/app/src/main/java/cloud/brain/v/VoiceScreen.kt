package cloud.brain.v

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * What "it should show on the screen" means.
 *
 * When the gate fires, this comes up over whatever was there — including the
 * lock screen. It is the Ambient surface from the brief, rendered on a phone
 * rather than on a device with no screen, and it is here for a reason beyond
 * looking finished:
 *
 * **An open microphone must be visible.** The session holds the mic for up to
 * eight seconds after V stops speaking, with no wake word required to continue.
 * That is the right interaction and it is also, precisely, the thing people are
 * afraid a device like this is doing behind their back. So while it is true, it
 * is on the screen, at full brightness, saying which state it is in — and one
 * tap ends it. The status-bar mic dot says *something* is recording; this says
 * what, and offers a way out.
 *
 * The aperture is the state, per the brief: no spinner, no separate indicator.
 *
 * ### Two pages, not one
 *
 * The *V Mobile* canvas splits this surface in two and puts a swipe between
 * them: the field on the first page with nothing competing with it, the
 * transcript on the second. The old single page had the log squeezed into a
 * 200dp box under the mark, which made a long answer both un-readable *and* a
 * distraction from the thing that is actually speaking. Neither page is a
 * different mode — the session runs identically on both, and the mark in the
 * transcript header is the same component at 62dp.
 */
@Composable
fun VoiceScreen(onDone: () -> Unit) {
    val phase by VoiceSession.phase.collectAsState()
    val caption by VoiceSession.caption.collectAsState()
    val cost by VoiceSession.cost.collectAsState()
    val lines by VoiceSession.lines.collectAsState()
    val levels by VoiceSession.levels.collectAsState()

    val ambient = ambientOf(phase)
    val label = labelOf(phase)
    val end = { VoiceSession.close(); onDone() }

    val pager = rememberPagerState { 2 }

    HorizontalPager(
        state = pager,
        modifier = Modifier
            .fillMaxSize()
            .background(V.void)
            .statusBarsPadding()
            .navigationBarsPadding(),
    ) { page ->
        if (page == 0) {
            Field(ambient, label, caption, cost, levels, phase, pager.currentPage, end)
        } else {
            Transcript(ambient, label, cost, lines, levels, end)
        }
    }
}

// ---- page one: the field --------------------------------------------------

/**
 * The mark, at the size the canvas draws it, with nothing above it but the
 * state's own name.
 *
 * Everything on this page below the aperture is subordinate to it: the bars are
 * the microphone, the caption is the sentence, the cost line is the price. None
 * of them is a control. The only control is the circle at the bottom, and the
 * only thing it does is stop.
 */
@Composable
private fun Field(
    ambient: Ambient,
    label: String,
    caption: String,
    cost: String,
    levels: FloatArray,
    phase: VoiceSession.Phase,
    page: Int,
    onEnd: () -> Unit,
) {
    Column(
        Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = 20.dp, end = 20.dp, top = 14.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(label, style = mono(10.sp, V.dim2, .2.em))
            Text("“Friday”", style = mono(10.sp, V.dim1, .2.em))
        }

        BoxWithConstraints(
            Modifier.weight(1f).fillMaxWidth(),
            contentAlignment = Alignment.Center,
        ) {
            // 340dp is the canvas's stage on a 412dp-wide frame. Every layer in
            // the mark is a fraction of it, so clamping the stage to a narrower
            // phone keeps the whole composition in proportion rather than
            // clipping the outermost ring off the edge.
            Aperture(
                state = ambient,
                stage = minOf(340.dp, maxWidth, maxHeight),
                activeTools = if (ambient == Ambient.THINKING) 2 else 1)
        }

        Waveform(levels, ambient)

        // A fixed block, so the mark does not jump up the screen every time a
        // one-word answer follows a three-line one.
        Column(
            Modifier
                .fillMaxWidth()
                .heightIn(min = 96.dp)
                .padding(start = 26.dp, end = 26.dp, top = 22.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                caption.ifBlank { if (phase == VoiceSession.Phase.THINKING) "…" else "" },
                fontFamily = V.sans, fontSize = 17.sp, lineHeight = 27.sp,
                color = if (phase == VoiceSession.Phase.SPEAKING) V.paperHi else V.paper,
                textAlign = TextAlign.Center)
            Spacer(Modifier.height(14.dp))
            Text(cost, style = mono(10.5.sp, V.dim2, .04.em), textAlign = TextAlign.Center)
        }

        Column(
            Modifier
                .fillMaxWidth()
                .padding(start = 20.dp, end = 20.dp, top = 18.dp, bottom = 10.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Composer(
                // The copy is the app's own rather than the canvas's "hold to
                // speak · release to send": this session is hands-free between
                // turns, and a hint that describes a gesture the app does not
                // have would be a lie told in the one place whose whole job is
                // to be honest about the microphone.
                hint = when (phase) {
                    VoiceSession.Phase.LISTENING -> "listening · no wake word needed"
                    VoiceSession.Phase.THINKING -> "working · the mic is closed"
                    else -> "the mic stays open between turns"
                },
                listening = phase == VoiceSession.Phase.LISTENING,
                onEnd = onEnd)

            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (i in 0 until 2) {
                    Box(
                        Modifier
                            .size(width = 22.dp, height = 3.dp)
                            .background(if (i == page) V.seed else V.line2,
                                        RoundedCornerShape(2.dp)))
                }
            }
            Spacer(Modifier.height(8.dp))
            Text("SWIPE FOR THE TRANSCRIPT", style = mono(10.sp, V.dim1, .14.em))
        }
    }
}

// ---- page two: the transcript ---------------------------------------------

/**
 * The turns so far, newest last, so a long answer is re-readable after it has
 * been spoken. This reads as a conversation, not a feed.
 *
 * The mark shrinks into the header rather than disappearing — the session is
 * still open on this page, and the one rule of this screen is that an open
 * microphone is never off-screen.
 */
@Composable
private fun Transcript(
    ambient: Ambient,
    label: String,
    cost: String,
    lines: List<VoiceSession.Line>,
    levels: FloatArray,
    onEnd: () -> Unit,
) {
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier
                .fillMaxWidth()
                .background(V.surface)
                .padding(start = 18.dp, end = 18.dp, top = 12.dp, bottom = 14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Aperture(state = ambient, stage = 62.dp)
            Spacer(Modifier.width(14.dp))
            Column(Modifier.weight(1f)) {
                Text(label, style = mono(9.5.sp, V.dim1, .2.em))
                Spacer(Modifier.height(4.dp))
                Text(cost, style = mono(10.sp, V.dim2, .04.em), maxLines = 1)
            }
            Spacer(Modifier.width(10.dp))
            MiniWaveform(levels, ambient)
        }
        Box(Modifier.fillMaxWidth().height(1.dp).background(Color(0xFF14141B)))

        Column(
            Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            if (lines.isEmpty()) {
                Text("Nothing said yet.", style = mono(11.sp, V.dim3))
            }
            for (line in lines) {
                if (line.fromV) VTurn(line) else UserTurn(line.text)
            }
        }

        Box(Modifier.fillMaxWidth().height(1.dp).background(Color(0xFF14141B)))
        Box(
            Modifier
                .fillMaxWidth()
                .background(V.surface)
                .padding(start = 16.dp, end = 16.dp, top = 10.dp, bottom = 14.dp),
        ) {
            Composer(hint = "swipe back for the field",
                     listening = ambient == Ambient.LISTENING,
                     onEnd = onEnd, framed = false)
        }
    }
}

@Composable
private fun UserTurn(text: String) {
    Row {
        Text("›", style = mono(V.transcript, V.muted))
        Spacer(Modifier.width(8.dp))
        Persianable(text, color = V.paperHi, mono = true)
    }
}

@Composable
private fun VTurn(line: VoiceSession.Line) {
    // Settle: the answer arrives by moving up 10dp and fading in over 320ms.
    // Never a typewriter — the brief forbids it, and a typewriter also makes a
    // fast local answer feel slower than the model it did not need.
    var landed by remember(line) { mutableStateOf(false) }
    LaunchedEffect(line) { landed = true }
    val rise by animateFloatAsState(if (landed) 0f else 10f,
        androidx.compose.animation.core.tween(V.ARRIVE_MS, easing = Arrive), label = "settle")
    val fade by animateFloatAsState(if (landed) 1f else 0f,
        androidx.compose.animation.core.tween(V.ARRIVE_MS, easing = Arrive), label = "fade")

    Column(Modifier.offset(y = rise.dp).alpha(fade)) {
        Row {
            Text(V.VOICE, style = mono(V.transcript, V.seed))
            Spacer(Modifier.width(9.dp))
            Persianable(line.text, color = V.paper, mono = true)
        }
        if (line.cost.isNotBlank()) {
            Text(line.cost,
                 style = mono(V.cost, V.dim2, .04.em),
                 modifier = Modifier.padding(start = V.hang, top = 5.dp))
        }
    }
}

// ---- the pieces both pages share ------------------------------------------

/**
 * The one control on the surface.
 *
 * It is drawn as the canvas's composer — the `›`, the hint, the circle — but
 * there is no text field behind it, because there is no typing during a voice
 * session. What the circle does is end the session, which is the affordance the
 * brief demands be within one tap at all times.
 */
@Composable
private fun Composer(hint: String, listening: Boolean, onEnd: () -> Unit,
                     framed: Boolean = true) {
    Row(
        Modifier
            .fillMaxWidth()
            .then(if (framed) Modifier
                .background(V.panel, RoundedCornerShape(14.dp))
                .border(1.dp, V.line, RoundedCornerShape(14.dp))
                .padding(horizontal = 14.dp, vertical = 10.dp)
            else Modifier),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("›", style = mono(14.sp, V.seed))
        Spacer(Modifier.width(12.dp))
        Text(hint, style = mono(V.transcript, V.dim1), modifier = Modifier.weight(1f))
        Box(
            Modifier
                .size(38.dp)
                .background(if (listening) V.seed.copy(alpha = .18f) else Color.Transparent,
                            CircleShape)
                .border(1.dp, if (listening) V.seed else V.line3, CircleShape)
                .clickable(onClick = onEnd),
            contentAlignment = Alignment.Center,
        ) {
            Text("◎", style = mono(15.sp, if (listening) V.seed else V.muted))
        }
    }
}

/** The header's five bars — the same levels as the field's twenty-one, read at
 *  a glance rather than watched. */
@Composable
private fun MiniWaveform(levels: FloatArray, ambient: Ambient) {
    val active = ambient == Ambient.LISTENING || ambient == Ambient.SPEAKING
    Row(
        Modifier.height(26.dp),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        // Spread across the twenty-one, so the five are a sample of the whole
        // window rather than only its newest fifth.
        for (i in 0 until 5) {
            val level = levels.getOrElse(i * 4 + 2) { 0f }
            val h by animateFloatAsState(
                if (active) 4f + level * 22f else 2f,
                androidx.compose.animation.core.tween(350, easing = Arrive), label = "mini-bar")
            Box(
                Modifier
                    .width(2.dp)
                    .height(h.dp)
                    .background(if (active) V.seed else V.line2, RoundedCornerShape(1.dp)))
        }
    }
}

private fun mono(size: androidx.compose.ui.unit.TextUnit, colour: Color,
                 tracking: androidx.compose.ui.unit.TextUnit = 0.sp) =
    TextStyle(fontFamily = V.mono, fontSize = size, color = colour, letterSpacing = tracking)

private fun ambientOf(phase: VoiceSession.Phase) = when (phase) {
    VoiceSession.Phase.WAKE -> Ambient.WAKE
    VoiceSession.Phase.LISTENING -> Ambient.LISTENING
    VoiceSession.Phase.THINKING -> Ambient.THINKING
    VoiceSession.Phase.SPEAKING -> Ambient.SPEAKING
    VoiceSession.Phase.IDLE -> Ambient.IDLE
}

private fun labelOf(phase: VoiceSession.Phase) = when (phase) {
    VoiceSession.Phase.WAKE -> "WAKE HEARD"
    VoiceSession.Phase.LISTENING -> "LISTENING"
    VoiceSession.Phase.THINKING -> "THINKING"
    VoiceSession.Phase.SPEAKING -> "SPEAKING"
    VoiceSession.Phase.IDLE -> "IDLE"
}
