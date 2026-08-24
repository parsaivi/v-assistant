package cloud.brain.v

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.compose.material3.Text

/**
 * §03 of the design brief, folded into one column.
 *
 * The terminal surface splits into a transcript and a side pane that never
 * scrolls with the log. A phone has no room for the second, so the four blocks
 * that lived there are redistributed rather than dropped: the aperture becomes
 * the header (it is the state, so it belongs where the state is read), and the
 * session and budget rows collapse into the one status line beneath it.
 *
 * Everything the brief calls load-bearing survives intact:
 *
 *  - `✦` prefixes everything V says, in one of four colours, and appears
 *    nowhere else. The aperture and ✦ never appear together — the header
 *    aperture is chrome, and the transcript is where V speaks.
 *  - The cost line is under every answer, always. Not a setting.
 *  - Persian renders in a bidi block that owns its direction, never inline.
 *  - Answers *settle* — translateY 10→0 over 320ms. Never a typewriter.
 *  - Red is refusal. Amber is error. They are never the same colour.
 */
@Composable
fun ChatScreen(model: VModel, onCalibrate: () -> Unit = {}) {
    val turns by model.turns.collectAsState()
    val ambient by model.ambient.collectAsState()
    val status by model.status.collectAsState()
    val notes by model.notes.collectAsState()
    val fault by model.fault.collectAsState()
    val levels by model.levels.collectAsState()

    var input by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    LaunchedEffect(turns.size) {
        if (turns.isNotEmpty()) listState.animateScrollToItem(turns.size - 1)
    }

    Column(
        Modifier
            .fillMaxSize()
            .background(V.void)
            .statusBarsPadding()
            .imePadding()
    ) {
        Header(ambient, status, notes, levels,
               onHush = { model.hush() },
               onNew = { model.newSession() },
               onCalibrate = onCalibrate)

        fault?.let { Fault(it) { model.clearFault(); model.refresh() } }

        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = V.gutter, vertical = 12.dp),
        ) {
            items(turns) { turn ->
                if (turn.mine) UserLine(turn.text) else AnswerBlock(turn, model)
            }
        }

        Composer(
            value = input,
            onValue = { input = it },
            onSend = {
                model.send(input)
                input = ""
            },
            onHoldStart = { model.startListening() },
            onHoldEnd = { model.stopListening() },
            listening = ambient == Ambient.LISTENING,
        )
    }
}

// ---- header ---------------------------------------------------------------
@Composable
private fun Header(ambient: Ambient, status: String, notes: Int,
                   levels: FloatArray, onHush: () -> Unit, onNew: () -> Unit,
                   onCalibrate: () -> Unit = {}) {
    Column(
        Modifier
            .fillMaxWidth()
            .background(V.surface)
            .padding(top = 10.dp, bottom = 12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            Modifier.pointerInput(Unit) {
                detectTapGestures(
                    // Tapping the mark stops him talking. It is the largest
                    // target on the screen and the only thing you would reach
                    // for mid-sentence, which is the whole argument for it
                    // being the control rather than a separate stop button.
                    onTap = { onHush() },
                    onLongPress = { onNew() })
            },
            contentAlignment = Alignment.Center,
        ) {
            Aperture(state = ambient, stage = 132.dp,
                     activeTools = if (ambient == Ambient.THINKING) 2 else 1)
        }

        Waveform(levels, ambient)

        Spacer(Modifier.height(6.dp))
        Text(
            text = stateLabel(ambient),
            style = TextStyle(fontFamily = V.mono, fontSize = V.label,
                              letterSpacing = 0.2.em, color = V.dim1),
        )
        Spacer(Modifier.height(3.dp))
        // The metadata line is the way in to calibration. A settings icon in a
        // header the brief specifies down to the letter-spacing would be a
        // foreign object; this line already exists to say what the gate is
        // doing, so it is the honest place to hang "and here is where you
        // change it".
        Text(
            text = if (notes > 0) status else status.ifEmpty { "—" },
            style = TextStyle(fontFamily = V.mono, fontSize = V.meta, color = V.dim2),
            modifier = Modifier.pointerInput(Unit) {
                detectTapGestures(onLongPress = { onCalibrate() })
            },
        )
    }
}

private fun stateLabel(a: Ambient) = when (a) {
    Ambient.IDLE -> "I D L E"
    Ambient.WAKE -> "W A K E   H E A R D"
    Ambient.LISTENING -> "L I S T E N I N G"
    Ambient.THINKING -> "T H I N K I N G"
    Ambient.SPEAKING -> "S P E A K I N G"
    Ambient.MUTED -> "H A R D   M U T E D"
}

/**
 * 21 bars, 3dp wide, 5dp apart — driven by real RMS out of `Recorder`, which
 * is the one thing the brief says about this component that is not a number.
 */
@Composable
private fun Waveform(levels: FloatArray, ambient: Ambient) {
    val active = ambient == Ambient.LISTENING || ambient == Ambient.SPEAKING
    Row(
        Modifier
            .padding(top = 10.dp)
            .height(50.dp),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(5.dp),
    ) {
        for (i in 0 until Recorder.BARS) {
            val level = levels.getOrElse(i) { 0f }
            val height by animateFloatAsState(
                targetValue = if (active) 6f + level * 44f else 3f,
                animationSpec = tween(350, easing = Arrive),
                label = "bar")
            Box(
                Modifier
                    .width(3.dp)
                    .height(height.dp)
                    .background(
                        when {
                            ambient == Ambient.SPEAKING -> Color(0xFF9E86FF)
                            ambient == Ambient.LISTENING -> V.seed
                            ambient == Ambient.MUTED -> V.line2.copy(alpha = .5f)
                            else -> Color(0xFF2A2A36)
                        },
                        RoundedCornerShape(2.dp)))
        }
    }
}

// ---- the log --------------------------------------------------------------
@Composable
private fun UserLine(text: String) {
    Row(Modifier.padding(top = 14.dp)) {
        Text("›", style = TextStyle(fontFamily = V.mono, fontSize = V.transcript,
                                    color = V.muted))
        Spacer(Modifier.width(8.dp))
        Persianable(text, color = V.paperHi, mono = true)
    }
}

@Composable
private fun AnswerBlock(turn: Turn, model: VModel) {
    // Settle: the answer arrives by moving up 10dp and fading in over 320ms.
    // Never a typewriter — the brief forbids it, and a typewriter also makes a
    // fast local answer feel slower than the model it did not need.
    var landed by remember(turn) { mutableStateOf(false) }
    LaunchedEffect(turn) { landed = true }
    val rise by animateFloatAsState(
        targetValue = if (landed) 0f else 10f,
        animationSpec = tween(V.ARRIVE_MS, easing = Arrive), label = "settle")
    val fade by animateFloatAsState(
        targetValue = if (landed) 1f else 0f,
        animationSpec = tween(V.ARRIVE_MS, easing = Arrive), label = "fade")

    Column(
        Modifier
            .padding(top = 14.dp)
            .offset(y = rise.dp)
            .alpha(fade)
    ) {
        Row {
            Text(
                V.VOICE,
                style = TextStyle(fontFamily = V.mono, fontSize = V.transcript,
                                  color = V.voice(turn.state)))
            Spacer(Modifier.width(9.dp))
            Persianable(turn.text, color = V.paper, mono = true,
                        rtlAccent = V.confidence(turn.confidence))
        }

        // The cost line. Dim, always present, never behind a setting.
        Text(
            turn.costLine(),
            modifier = Modifier.padding(start = V.hang, top = 5.dp),
            style = TextStyle(fontFamily = V.mono, fontSize = V.cost,
                              letterSpacing = 0.04.sp,
                              color = if (turn.local) V.free.copy(alpha = .75f)
                                      else V.dim2))

        if (turn.citations.isNotEmpty()) {
            Column(Modifier.padding(start = V.hang, top = 4.dp)) {
                turn.citations.take(6).forEach {
                    Text("${V.NOTE} $it",
                         style = TextStyle(fontFamily = V.mono, fontSize = V.meta,
                                           color = V.dim3))
                }
            }
        }

        // Buttons exist because the moment of low confidence is the only moment
        // a button beats a sentence. Plain language must always work too, and
        // does — these are the same actions `engine.act` takes from a sentence.
        if (turn.buttons.isNotEmpty()) {
            Row(
                Modifier.padding(start = V.hang, top = 9.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                turn.buttons.forEach { b ->
                    Box(
                        Modifier
                            .border(1.dp, V.hoverLine, RoundedCornerShape(7.dp))
                            .background(Color(0xFF14141B), RoundedCornerShape(7.dp))
                            .pointerInput(b) {
                                detectTapGestures { model.press(b) }
                            }
                            .padding(horizontal = 12.dp, vertical = 7.dp)) {
                        Text(b.label,
                             style = TextStyle(fontFamily = V.mono,
                                               fontSize = V.small, color = V.bodyHi))
                    }
                }
            }
        }
    }
}

// ---- Persian --------------------------------------------------------------
private val PERSIAN = Regex("[\\u0600-\\u06FF\\u0750-\\u077F\\uFB50-\\uFDFF\\uFE70-\\uFEFF]")

/**
 * Persian gets a block, never an inline run.
 *
 * The terminal surface had to do the opposite — isolate per *run*, because
 * setting a whole terminal line RTL threw dates and metadata to the far left.
 * That was a constraint of a character grid, not a design decision, and the
 * brief's own rule for a real layout engine is the block: `dir=rtl`, IBM Plex
 * Sans Arabic, its own background, and a 2px right border in the confidence
 * colour. The surrounding chrome stays LTR because the block owns its
 * direction rather than infecting the paragraph it sits in.
 */
@Composable
private fun Persianable(text: String, color: Color, mono: Boolean,
                        rtlAccent: Color = V.line3) {
    val blocks = remember(text) { split(text) }
    Column {
        blocks.forEach { (chunk, rtl) ->
            if (rtl) {
                Box(
                    Modifier
                        .padding(vertical = 5.dp)
                        .fillMaxWidth()
                        .background(Color(0xFF12121A), RoundedCornerShape(V.blockRadius))
                        .border(1.dp, V.line3, RoundedCornerShape(V.blockRadius))
                        .padding(start = 14.dp, end = 12.dp, top = 11.dp, bottom = 11.dp)
                ) {
                    // Intrinsic height, so the 2dp accent rule is as tall as
                    // the text beside it rather than collapsing to nothing —
                    // `fillMaxHeight` in a Row with no fixed height measures
                    // against zero.
                    Row(Modifier.height(IntrinsicSize.Min)) {
                        Text(
                            chunk.trim(),
                            modifier = Modifier.weight(1f),
                            style = TextStyle(
                                fontFamily = V.arabic,
                                fontSize = V.persian,
                                lineHeight = 28.sp,
                                color = color,
                                textAlign = TextAlign.Right,
                                textDirection = TextDirection.Rtl))
                        Spacer(Modifier.width(10.dp))
                        Box(Modifier.width(2.dp).fillMaxHeight().background(rtlAccent))
                    }
                }
            } else if (chunk.isNotBlank()) {
                Text(
                    chunk.trim(),
                    style = TextStyle(
                        fontFamily = if (mono) V.mono else V.sans,
                        fontSize = V.transcript,
                        lineHeight = 24.sp,
                        color = color))
            }
        }
    }
}

/** Split into (text, isPersian) runs at line granularity — a line is the
 *  smallest thing that can own a direction without the block becoming
 *  confetti. */
private fun split(text: String): List<Pair<String, Boolean>> {
    val out = mutableListOf<Pair<String, Boolean>>()
    val buffer = StringBuilder()
    var mode: Boolean? = null
    for (line in text.lines()) {
        val rtl = PERSIAN.containsMatchIn(line) &&
                  PERSIAN.findAll(line).count() * 3 > line.count { !it.isWhitespace() }
        if (mode == null) mode = rtl
        if (rtl != mode) {
            out += buffer.toString() to mode
            buffer.clear()
            mode = rtl
        }
        buffer.append(line).append('\n')
    }
    if (buffer.isNotEmpty()) out += buffer.toString() to (mode ?: false)
    return out
}

// ---- faults ---------------------------------------------------------------
@Composable
private fun Fault(message: String, onRetry: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Color(0xFF1A1408))
            .pointerInput(Unit) { detectTapGestures { onRetry() } }
            .padding(horizontal = V.gutter, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Amber, not red. An error is a fault; a refusal is V holding a rule
        // you gave him. Colour them alike and the one you learn to ignore is
        // the refusal.
        Text("!", style = TextStyle(fontFamily = V.mono, fontSize = V.cost,
                                    color = V.unsure, fontWeight = FontWeight.SemiBold))
        Spacer(Modifier.width(9.dp))
        Text(message, modifier = Modifier.weight(1f),
             style = TextStyle(fontFamily = V.mono, fontSize = V.cost, color = V.unsure))
        Text("retry", style = TextStyle(fontFamily = V.mono, fontSize = V.meta,
                                        color = V.dim2))
    }
}

// ---- the composer ---------------------------------------------------------
@Composable
private fun Composer(value: String, onValue: (String) -> Unit, onSend: () -> Unit,
                     onHoldStart: () -> Unit, onHoldEnd: () -> Unit,
                     listening: Boolean) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(V.surface)
            .padding(horizontal = V.gutter, vertical = 10.dp)
            .navigationBarsPadding(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("›", style = TextStyle(fontFamily = V.mono, fontSize = V.bodyText,
                                    color = V.seed))
        Spacer(Modifier.width(10.dp))
        Box(Modifier.weight(1f)) {
            if (value.isEmpty()) {
                Text("ask, or hold to speak",
                     style = TextStyle(fontFamily = V.mono, fontSize = V.transcript,
                                       color = V.dim3))
            }
            BasicTextField(
                value = value,
                onValueChange = onValue,
                textStyle = TextStyle(fontFamily = V.mono, fontSize = V.transcript,
                                      color = V.paperHi),
                cursorBrush = SolidColor(V.seed),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { onSend() }),
                modifier = Modifier.fillMaxWidth())
        }
        Spacer(Modifier.width(12.dp))

        // Hold to speak. A press-and-hold rather than a toggle because the
        // finger is a better endpoint detector than any silence threshold, and
        // because a mic that is on until you remember to turn it off is not a
        // mic this design would ship.
        Box(
            Modifier
                .size(38.dp)
                .background(
                    if (listening) V.seed.copy(alpha = .18f) else Color.Transparent,
                    RoundedCornerShape(50))
                .border(1.dp, if (listening) V.seed else V.line3, RoundedCornerShape(50))
                .pointerInput(Unit) {
                    detectTapGestures(
                        onPress = {
                            onHoldStart()
                            tryAwaitRelease()
                            onHoldEnd()
                        },
                        onTap = { if (value.isNotEmpty()) onSend() })
                },
            contentAlignment = Alignment.Center,
        ) {
            Text(if (listening) V.LOCAL else "◎",
                 style = TextStyle(fontFamily = V.mono, fontSize = 15.sp,
                                   color = if (listening) V.seed else V.muted))
        }
    }
}
