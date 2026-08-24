package cloud.brain.v

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
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
 */
@Composable
fun VoiceScreen(onDone: () -> Unit) {
    val phase by VoiceSession.phase.collectAsState()
    val caption by VoiceSession.caption.collectAsState()
    val cost by VoiceSession.cost.collectAsState()
    val lines by VoiceSession.lines.collectAsState()

    val ambient = when (phase) {
        VoiceSession.Phase.WAKE -> Ambient.WAKE
        VoiceSession.Phase.LISTENING -> Ambient.LISTENING
        VoiceSession.Phase.THINKING -> Ambient.THINKING
        VoiceSession.Phase.SPEAKING -> Ambient.SPEAKING
        VoiceSession.Phase.IDLE -> Ambient.IDLE
    }
    val label = when (phase) {
        VoiceSession.Phase.WAKE -> "WAKE HEARD"
        VoiceSession.Phase.LISTENING -> "LISTENING"
        VoiceSession.Phase.THINKING -> "THINKING"
        VoiceSession.Phase.SPEAKING -> "SPEAKING"
        VoiceSession.Phase.IDLE -> "IDLE"
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(V.void)
            .padding(horizontal = 24.dp, vertical = 20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, fontFamily = V.mono, fontSize = 10.sp,
                 letterSpacing = 0.4.sp, color = V.dim2)
            Text("“Friday”", fontFamily = V.mono, fontSize = 10.sp,
                 letterSpacing = 0.4.sp, color = V.dim3)
        }

        Spacer(Modifier.weight(1f))
        Aperture(state = ambient, stage = 280.dp)
        Spacer(Modifier.height(26.dp))

        Text(
            caption.ifBlank { if (phase == VoiceSession.Phase.LISTENING) "…" else "" },
            fontFamily = V.sans, fontSize = 17.sp, lineHeight = 27.sp,
            color = if (phase == VoiceSession.Phase.SPEAKING) V.paperHi else V.paper,
            textAlign = TextAlign.Center,
            modifier = Modifier.widthIn(max = 520.dp))

        Spacer(Modifier.height(14.dp))
        Text(cost, fontFamily = V.mono, fontSize = 10.5.sp,
             letterSpacing = 0.4.sp, color = V.dim2, textAlign = TextAlign.Center)

        Spacer(Modifier.weight(1f))

        // The turns so far, quietly, so a long answer is re-readable after it
        // has been spoken. Newest last — this reads as a conversation, not a
        // feed.
        AnimatedVisibility(lines.isNotEmpty()) {
            Column(
                Modifier
                    .fillMaxWidth()
                    .heightIn(max = 200.dp)
                    .background(V.panel, RoundedCornerShape(14.dp))
                    .padding(14.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                for (l in lines) {
                    Text(
                        l.text,
                        fontFamily = V.sans, fontSize = 13.sp, lineHeight = 20.sp,
                        color = if (l.fromV) V.body else V.bodyHi,
                        modifier = Modifier.fillMaxWidth(),
                        textAlign = if (l.fromV) TextAlign.Start else TextAlign.End)
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        TextButton(onClick = { VoiceSession.close(); onDone() }) {
            Text("Done", fontFamily = V.sans, fontSize = 14.sp, color = V.muted)
        }
    }
}
