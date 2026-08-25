package cloud.brain.v

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Turning the shadow log into two numbers.
 *
 * Without this screen the feature does not work. The gate ships in shadow mode,
 * scores everything, acts on nothing, and there was no way to see what it
 * scored or to leave shadow mode — the thresholds had setters with no callers
 * and the log had a reader with no reader. Everything up to here was a
 * measurement device with no dial.
 *
 * Two ways to set the dial, and the second is the one that actually works:
 *
 *  1. **The log**, plotted as two strips of dots. It shows the shape of what
 *     the phone has been hearing, and whether the wake scores separate into the
 *     two clusters they are supposed to.
 *  2. **A live probe.** Press it, say "Friday", and see the two scores that
 *     utterance produced and whether it would have fired. Reading a week of
 *     numbers tells you the distributions but never which entry was which;
 *     this closes the loop in one second, and it is why `Enrollment.probe`
 *     mirrors `Gate.evaluate` exactly rather than approximately.
 *
 * The headline number is "would have fired: n of m". Threshold-setting is not
 * an abstract judgement about cosine similarity — it is the question of how
 * much of what the phone already heard gets through, and that is the number
 * shown.
 */
@Composable
fun CalibrateScreen(onDone: () -> Unit, onReenroll: () -> Unit) {
    val context = LocalContext.current
    val prints = remember { Voiceprint(context) }
    val scope = rememberCoroutineScope()

    var entries by remember { mutableStateOf(prints.log()) }
    var wakeT by remember { mutableFloatStateOf(prints.wakeThreshold) }
    var speakerT by remember { mutableFloatStateOf(prints.speakerThreshold) }
    var shadow by remember { mutableStateOf(prints.shadowMode) }

    var listening by remember { mutableStateOf(prints.listeningEnabled) }
    var probing by remember { mutableStateOf(false) }
    var probe by remember { mutableStateOf<Enrollment.Probe?>(null) }
    var probeNote by remember { mutableStateOf("") }

    // How many of the logged candidates these thresholds would let through.
    val firing = entries.count { it.wake >= wakeT && (it.speaker ?: -1f) >= speakerT }

    fun persist() {
        prints.wakeThreshold = wakeT
        prints.speakerThreshold = speakerT
    }

    Column(
        Modifier
            .fillMaxSize()
            .background(V.void)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 22.dp, vertical = 18.dp),
    ) {
        Text("CALIBRATE", fontFamily = V.mono, fontSize = 10.sp,
             letterSpacing = 0.4.sp, color = V.dim2)
        Spacer(Modifier.height(4.dp))
        Text(
            if (shadow) "Shadow mode. Everything is scored, nothing acts."
            else "Live. The gate is acting on these thresholds.",
            fontFamily = V.sans, fontSize = 15.sp, lineHeight = 23.sp,
            color = if (shadow) V.unsure else V.free)

        Spacer(Modifier.height(6.dp))
        Text("${entries.size} candidates logged · would fire: $firing",
             fontFamily = V.mono, fontSize = 10.5.sp, color = V.dim2)

        // ---- the off switch, first and unmissable ----
        //
        // This was originally only an action on the ongoing notification, which
        // is a channel at IMPORTANCE_MIN — Android collapses those and hides
        // their buttons, so in practice there was no way to stop the
        // microphone at all. A thing that listens all day needs its stop to be
        // the most findable control in the app, not the most clever one.
        Spacer(Modifier.height(22.dp))
        Section("Microphone")
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Button(
                onClick = {
                    listening = !listening
                    prints.listeningEnabled = listening
                    context.startService(
                        android.content.Intent(context, VService::class.java)
                            .setAction(if (listening) VService.ACTION_LISTEN
                                       else VService.ACTION_STOP_LISTENING))
                },
                colors = ButtonDefaults.buttonColors(
                    containerColor = if (listening) V.refuse else V.free)) {
                Text(if (listening) "Stop listening" else "Start listening",
                     fontFamily = V.sans, fontWeight = FontWeight.Medium,
                     color = V.void)
            }
            Text(if (listening) "the mic is open" else "the mic is off",
                 fontFamily = V.mono, fontSize = 10.5.sp,
                 color = if (listening) V.body else V.dim1)
        }

        // ---- the live probe, which is the useful half ----
        Spacer(Modifier.height(20.dp))
        Section("Test it now")
        Text("Press, then say “Friday”. These are the scores the gate would have "
             + "computed for that utterance.",
             fontFamily = V.sans, fontSize = 13.sp, lineHeight = 20.sp, color = V.muted)
        Spacer(Modifier.height(10.dp))

        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Button(
                enabled = !probing,
                onClick = {
                    probing = true; probe = null; probeNote = "listening…"
                    scope.launch {
                        val (w, s) = withContext(Dispatchers.Default) {
                            WakeWord(context) to SpeakerId(context)
                        }
                        try {
                            val e = Enrollment(context, prints)
                            val p = withContext(Dispatchers.Default) { e.probe(w, s) }
                            probe = p
                            probeNote = when {
                                p == null -> "nothing heard"
                                p.tooShort -> "too short to be the word"
                                else -> ""
                            }
                        } catch (ex: Exception) {
                            probeNote = ex.message ?: "probe failed"
                        } finally {
                            withContext(Dispatchers.Default) { w.close(); s.close() }
                            probing = false
                        }
                    }
                },
                colors = ButtonDefaults.buttonColors(containerColor = V.seed)) {
                Text(if (probing) "Listening…" else "Say “Friday”",
                     fontFamily = V.sans, fontWeight = FontWeight.Medium)
            }
            if (probeNote.isNotBlank()) {
                Text(probeNote, fontFamily = V.mono, fontSize = 10.5.sp, color = V.dim2)
            }
        }

        probe?.let { p ->
            if (!p.tooShort) {
                val pass = p.wake >= wakeT && (p.speaker ?: -1f) >= speakerT
                Spacer(Modifier.height(12.dp))
                Column(
                    Modifier
                        .fillMaxWidth()
                        .background(V.panel, RoundedCornerShape(12.dp))
                        .padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    ScoreRow("wake word", p.wake, wakeT)
                    ScoreRow("your voice", p.speaker ?: 0f, speakerT)
                    Text(
                        if (pass) "→ would have woken V" else "→ would have been ignored",
                        fontFamily = V.mono, fontSize = 11.sp,
                        color = if (pass) V.free else V.dim1)
                    Text("${p.durationMs} ms", fontFamily = V.mono,
                         fontSize = 10.sp, color = V.dim3)
                }
            }
        }

        // ---- the two dials ----
        Spacer(Modifier.height(24.dp))
        Section("Wake word")
        Distribution(entries.map { it.wake }, wakeT, V.seed)
        Slider(value = wakeT, onValueChange = { wakeT = it }, valueRange = 0f..1f,
               colors = SliderDefaults.colors(thumbColor = V.seed,
                                              activeTrackColor = V.deep))
        Text("%.2f — lower catches more, and lets more through".format(wakeT),
             fontFamily = V.mono, fontSize = 10.5.sp, color = V.dim2)

        Spacer(Modifier.height(18.dp))
        Section("Your voice")
        Distribution(entries.mapNotNull { it.speaker }, speakerT, V.free)
        Slider(value = speakerT, onValueChange = { speakerT = it }, valueRange = 0f..1f,
               colors = SliderDefaults.colors(thumbColor = V.free,
                                              activeTrackColor = V.free.copy(alpha = .5f)))
        Text("%.2f — raise this until other voices stop scoring above it".format(speakerT),
             fontFamily = V.mono, fontSize = 10.5.sp, color = V.dim2)

        // ---- committing ----
        // Going live is deliberately *not* the loudest thing here, and that is
        // a correction. It used to be the one filled button on a screen you
        // land on straight after enrolling, with an empty log and two default
        // thresholds — so the obvious action was to leave shadow mode having
        // measured nothing, which is exactly what shadow mode exists to
        // prevent. It stays reachable, and says what it is committing to.
        Spacer(Modifier.height(26.dp))
        val measured = entries.size >= 20 || probe != null
        Button(
            enabled = !shadow || measured,
            onClick = {
                persist()
                shadow = !shadow
                prints.shadowMode = shadow
                VService.start(context)
                if (!shadow) onDone()
            },
            colors = ButtonDefaults.buttonColors(
                containerColor = if (!shadow) V.line3 else if (measured) V.seed else V.line2,
                disabledContainerColor = V.line2)) {
            Text(if (shadow) "Start acting on these" else "Back to shadow mode",
                 fontFamily = V.sans, fontWeight = FontWeight.Medium,
                 color = if (shadow && !measured) V.dim1 else V.paperHi)
        }
        if (shadow && !measured) {
            Spacer(Modifier.height(6.dp))
            Text("Probe once, or let it listen for a while first — going live on "
                 + "two untested numbers is how this ends up ignoring you.",
                 fontFamily = V.sans, fontSize = 12.5.sp, lineHeight = 19.sp,
                 color = V.dim1)
        }

        Spacer(Modifier.height(6.dp))
        Row {
            TextButton(onClick = { persist(); onDone() }) {
                Text("Save and close", fontFamily = V.sans, color = V.muted)
            }
            TextButton(onClick = { prints.clearLog(); entries = prints.log() }) {
                Text("Clear log", fontFamily = V.sans, color = V.dim1)
            }
        }

        Spacer(Modifier.height(18.dp))
        Section("Voiceprint")
        Text("Re-record if the gate stopped recognising you — a cold, a new "
             + "phone, or a room that sounds different.",
             fontFamily = V.sans, fontSize = 13.sp, lineHeight = 20.sp, color = V.muted)
        TextButton(onClick = { prints.forget(); onReenroll() }) {
            Text("Record it again", fontFamily = V.sans, color = V.unsure)
        }
        Spacer(Modifier.height(30.dp))
    }
}

@Composable
private fun Section(label: String) {
    Text(label.uppercase(), fontFamily = V.mono, fontSize = 9.5.sp,
         letterSpacing = 0.5.sp, color = V.dim1)
    Spacer(Modifier.height(6.dp))
}

@Composable
private fun ScoreRow(label: String, value: Float, threshold: Float) {
    val ok = value >= threshold
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, fontFamily = V.sans, fontSize = 13.sp, color = V.body)
        Text("%.3f  %s  %.2f".format(value, if (ok) "≥" else "<", threshold),
             fontFamily = V.mono, fontSize = 11.sp,
             color = if (ok) V.free else V.refuse)
    }
}

/**
 * Every logged score as a dot on a 0..1 axis, with the threshold as a line.
 *
 * A histogram would bin away the thing being looked for. What matters is
 * whether the scores fall into two separated clusters and where the gap
 * between them is — with a few hundred points, drawing them individually shows
 * that directly, and shows an overlap that binning would smooth over.
 */
@Composable
private fun Distribution(values: List<Float>, threshold: Float, tint: Color) {
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(54.dp)
            .background(V.panel, RoundedCornerShape(8.dp))
            .padding(horizontal = 8.dp, vertical = 6.dp)) {
        val w = size.width
        val h = size.height
        // A faint baseline, so an empty log reads as "nothing yet" rather than
        // as a broken component.
        drawLine(V.line2, Offset(0f, h / 2), Offset(w, h / 2), strokeWidth = 1f)
        values.forEachIndexed { i, v ->
            val x = v.coerceIn(0f, 1f) * w
            // Deterministic vertical jitter so overlapping scores stay visible
            // as density rather than stacking into one dot.
            val y = h * (0.25f + 0.5f * (((i * 37) % 100) / 100f))
            drawCircle(tint.copy(alpha = 0.55f), radius = 2.2f, center = Offset(x, y))
        }
        val tx = threshold.coerceIn(0f, 1f) * w
        drawLine(V.paperHi, Offset(tx, 0f), Offset(tx, h), strokeWidth = 1.6f)
    }
    Spacer(Modifier.height(4.dp))
}
