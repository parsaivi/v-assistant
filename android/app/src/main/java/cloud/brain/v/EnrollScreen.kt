package cloud.brain.v

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * Teaching V the sound of his name, fifteen times.
 *
 * The brief bans onboarding — "no tour, no sample questions" — and this is the
 * one screen that has to violate the spirit of that, because the gate cannot
 * work without it. So it is built to be the smallest possible violation: one
 * instruction, a counter, no explanation of what an embedding is, and it never
 * appears again once it has what it needs.
 *
 * The aperture is the progress bar. There is no second indicator, because the
 * mark is already the thing that says whether V is hearing him.
 */
@Composable
fun EnrollScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val prints = remember { Voiceprint(context) }
    val enrollment = remember { Enrollment(context, prints) }

    var captured by remember { mutableStateOf(0) }
    var running by remember { mutableStateOf(false) }
    var caption by remember { mutableStateOf("Say “Friday”, the way you would say it to him.") }
    var meta by remember { mutableStateOf("not started") }
    var finished by remember { mutableStateOf(false) }

    DisposableEffect(Unit) { onDispose { } }

    LaunchedEffect(running) {
        if (!running) return@LaunchedEffect
        val wake = withContext(Dispatchers.Default) { WakeWord(context) }
        val speaker = withContext(Dispatchers.Default) { SpeakerId(context) }
        try {
            while (captured < Voiceprint.ENROLL_TARGET) {
                caption = "Say “Friday”."
                meta = "listening · take ${captured + 1} of ${Voiceprint.ENROLL_TARGET}"
                val s = enrollment.capture(wake, speaker)
                if (s == null) {
                    caption = "Didn’t catch that — once more."
                    meta = "take ${captured + 1} not counted"
                    delay(700)
                    continue
                }
                captured = enrollment.count
                caption = "Good."
                meta = "${s.durationMs} ms captured"
                delay(450)
            }
            val r = withContext(Dispatchers.Default) { enrollment.commit() }
            // Shadow mode stays on after enrolling. The gate will score
            // everything and act on nothing until the log says what the
            // thresholds should be — see the design note.
            prints.shadowMode = true
            finished = true
            running = false
            caption = "Done. V knows your voice."
            meta = if (r.dropped == 0)
                "${r.kept} takes kept · cohesion ${"%.2f".format(r.cohesion)}"
            else
                "${r.kept} kept · ${r.dropped} discarded as outliers · " +
                "cohesion ${"%.2f".format(r.cohesion)}"
        } catch (e: Exception) {
            running = false
            caption = "Enrollment failed."
            meta = e.message ?: "unknown"
        } finally {
            withContext(Dispatchers.Default) { wake.close(); speaker.close() }
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(V.void)
            .padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("ENROLL", fontFamily = V.mono, fontSize = 10.sp,
             letterSpacing = 0.4.sp, color = V.dim2)

        Spacer(Modifier.height(24.dp))
        Aperture(
            state = if (running) Ambient.LISTENING else if (finished) Ambient.SPEAKING
                    else Ambient.IDLE,
            stage = 260.dp)
        Spacer(Modifier.height(24.dp))

        Text(caption, fontFamily = V.sans, fontSize = 16.sp, lineHeight = 26.sp,
             color = V.paperHi, textAlign = TextAlign.Center,
             modifier = Modifier.widthIn(max = 420.dp))
        Spacer(Modifier.height(14.dp))
        Text(meta, fontFamily = V.mono, fontSize = 10.5.sp,
             letterSpacing = 0.4.sp, color = V.dim2, textAlign = TextAlign.Center)

        Spacer(Modifier.height(28.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            repeat(Voiceprint.ENROLL_TARGET) { i ->
                Box(Modifier
                    .size(if (i < captured) 7.dp else 5.dp)
                    .background(if (i < captured) V.seed else V.line3,
                                RoundedCornerShape(4.dp)))
            }
        }

        Spacer(Modifier.height(36.dp))
        if (finished) {
            Button(onClick = onDone,
                   colors = ButtonDefaults.buttonColors(containerColor = V.seed)) {
                Text("Start listening", fontFamily = V.sans,
                     fontWeight = FontWeight.Medium)
            }
        } else if (!running) {
            Button(onClick = { enrollment.reset(); captured = 0; running = true },
                   colors = ButtonDefaults.buttonColors(containerColor = V.seed)) {
                Text("Begin", fontFamily = V.sans, fontWeight = FontWeight.Medium)
            }
        }
    }
}
