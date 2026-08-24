package cloud.brain.v

import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * The mark, and the state machine, which are the same object.
 *
 * > "The mark *is* the state. The dashed ring's rotation period and the core
 * > dot's diameter are bound to V's state. Implement it as one component that
 * > takes a state, not as a static asset plus separate animations."
 *
 * So there is no drawable and no Lottie file here: three circles drawn on a
 * canvas, whose geometry never changes and whose motion is read off `state`.
 * The device, the app icon and this are the same object at different sizes —
 * `res/drawable/ic_aperture_fg.xml` is the same three circles with the dash
 * dropped, per the 16px reduction rule.
 */
enum class Ambient { IDLE, WAKE, LISTENING, THINKING, SPEAKING, MUTED }

/** The per-state numbers, quoted from the brief's Ambient table. */
private data class Spec(
    val spinSeconds: Float,     // dashed ring rotation period; 0 = still
    val coreDp: Float,          // core dot diameter at the 152dp reference
    val glow: Float,            // drop-shadow alpha
    val halo: Float,            // halo alpha
    val breatheMs: Int,         // core dot pulse; 0 = none
)

private fun spec(state: Ambient) = when (state) {
    Ambient.IDLE      -> Spec(26.0f, 26f, .22f, .14f, 5000)
    Ambient.WAKE      -> Spec(3.4f, 34f, .55f, .40f, 1400)
    Ambient.LISTENING -> Spec(6.0f, 30f, .60f, .42f, 2200)
    Ambient.THINKING  -> Spec(0f, 16f, .42f, .30f, 2200)
    Ambient.SPEAKING  -> Spec(2.2f, 42f, .70f, .50f, 1100)
    // A state no software path can produce has to look like one: flat unlit
    // grey, zero glow, zero animation. Nothing else in the system is still.
    Ambient.MUTED     -> Spec(0f, 22f, 0f, 0f, 0)
}

@Composable
fun Aperture(
    state: Ambient,
    modifier: Modifier = Modifier,
    /** One gyroscope ring per tool in flight. A one-step answer spins one
     *  ring; a four-tool answer visibly spins more. Never three for a lookup. */
    activeTools: Int = 1,
    reducedMotion: Boolean = false,
    /** The square the whole composition is drawn into. The brief's reference
     *  is a 300px stage holding a 152px aperture; scaling the stage scales
     *  everything in it by the same ratio, so the proportions the mark is
     *  specified in survive being put in a phone's header. */
    stage: Dp = 300.dp,
) {
    val s = spec(state)
    val k = stage / 300.dp
    // 152dp normally, 92dp while thinking — so the gyroscope reads as bigger
    // than him. Animated, because size is information and survives reduced
    // motion; only the loops stop there.
    val diameter by animateDpAsState(
        targetValue = (if (state == Ambient.THINKING) 92.dp else 152.dp) * k,
        animationSpec = tween(550, easing = Arrive),
        label = "aperture-size")

    val core by animateDpAsState(
        targetValue = s.coreDp.dp * k,
        animationSpec = tween(V.STATE_MS, easing = Arrive),
        label = "core-size")

    val loop = rememberInfiniteTransition(label = "aperture")

    val spin by if (s.spinSeconds > 0f && !reducedMotion) {
        loop.animateFloat(
            0f, 360f,
            infiniteRepeatable(tween((s.spinSeconds * 1000).toInt(), easing = LinearEasing)),
            label = "spin")
    } else rememberStatic(0f)

    val breath by if (s.breatheMs > 0 && !reducedMotion) {
        loop.animateFloat(
            1f, 1.045f,
            infiniteRepeatable(tween(s.breatheMs / 2, easing = Breathe),
                               RepeatMode.Reverse),
            label = "breathe")
    } else rememberStatic(1f)

    val haloPulse by if (s.halo > 0f && !reducedMotion) {
        loop.animateFloat(
            1f, 1.18f,
            infiniteRepeatable(tween(2500, easing = Breathe), RepeatMode.Reverse),
            label = "halo")
    } else rememberStatic(1f)

    val gyro by if (state == Ambient.THINKING && !reducedMotion) {
        loop.animateFloat(0f, 360f,
            infiniteRepeatable(tween(2600, easing = LinearEasing)), label = "gyro")
    } else rememberStatic(0f)

    Box(modifier = modifier.size(stage), contentAlignment = Alignment.Center) {

        // ---- halo ---------------------------------------------------------
        if (s.halo > 0f) {
            Canvas(Modifier.size(260.dp * k)) {
                val r = size.minDimension / 2f * haloPulse
                drawCircle(
                    brush = Brush.radialGradient(
                        colors = listOf(V.seed.copy(alpha = s.halo), Color.Transparent),
                        center = center, radius = r),
                    radius = r, center = center)
            }
        }

        // ---- the thinking gyroscope --------------------------------------
        // Three rings on differing axes. The single contrasting border edge on
        // the first two is what makes the rotation legible as 3D — uniform
        // rings read as flat circles with nothing happening.
        if (state == Ambient.THINKING) {
            Canvas(Modifier.size(238.dp * k)) {
                val rings = activeTools.coerceIn(1, 3)
                for (i in 0 until rings) {
                    val inset = i * 22.dp.toPx() * k
                    val speed = when (i) { 0 -> 1f; 1 -> 0.67f; else -> 0.42f }
                    val squash = when (i) { 0 -> 0.26f; 1 -> 0.31f; else -> 0.88f }
                    rotate(gyro * speed) {
                        drawOval(
                            color = V.seed.copy(alpha = .45f),
                            topLeft = Offset(inset, inset + (size.height - inset * 2) *
                                    (1 - squash) / 2f),
                            size = Size(size.width - inset * 2,
                                        (size.height - inset * 2) * squash),
                            style = Stroke(width = 1.dp.toPx()))
                    }
                }
            }
        }

        // ---- the aperture itself ------------------------------------------
        Canvas(Modifier.size(diameter)) {
            val d = size.minDimension
            // While thinking, the rings fade back and stop — the gyroscope is
            // the subject and he is not.
            val ringAlpha = if (state == Ambient.THINKING) .28f else 1f
            val dashAlpha = if (state == Ambient.THINKING) .2f else 1f
            val stroke = 2.5f * d / 152f

            drawCircle(
                color = ringColour(state, V.deep).copy(alpha = ringAlpha),
                radius = (d - stroke) / 2f,
                style = Stroke(width = stroke))

            rotate(spin) {
                drawCircle(
                    color = ringColour(state, V.seed).copy(alpha = dashAlpha),
                    radius = d / 2f - d * .17f,
                    style = Stroke(
                        width = stroke,
                        pathEffect = PathEffect.dashPathEffect(
                            floatArrayOf(d * .055f, d * .045f))))
            }

            drawCore(state, core.toPx() * breath / 2f)
        }
    }
}

/** Muted is the one state drawn from a different palette — flat unlit grey. */
private fun ringColour(state: Ambient, normal: Color): Color =
    if (state == Ambient.MUTED) V.line3 else normal

private fun DrawScope.drawCore(state: Ambient, radius: Float) {
    if (state == Ambient.MUTED) {
        drawCircle(color = V.line2, radius = radius, center = center)
        return
    }
    drawCircle(
        brush = Brush.radialGradient(
            colors = listOf(V.coreHi, Color(0xFF8E75FF), V.deep),
            // The highlight sits at 34%/30%, not at the centre: a dot lit from
            // one side reads as a sphere, and a centred gradient reads as a
            // flat disc with a bright middle.
            center = Offset(center.x - radius * .32f, center.y - radius * .40f),
            radius = radius * 1.6f),
        radius = radius, center = center)
}

// ---- the two curves, and only two ---------------------------------------
/** `cubic-bezier(.2,.8,.2,1)` — anything arriving. */
val Arrive = CubicBezierEasing(.2f, .8f, .2f, 1f)
/** `ease-in-out` — anything breathing. Nothing bounces. */
val Breathe = CubicBezierEasing(.42f, 0f, .58f, 1f)

@Composable
private fun rememberStatic(value: Float) =
    androidx.compose.runtime.remember(value) {
        androidx.compose.runtime.mutableFloatStateOf(value)
    }
