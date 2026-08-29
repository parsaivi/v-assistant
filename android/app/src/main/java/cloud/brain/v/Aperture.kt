package cloud.brain.v

import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.InfiniteTransition
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.PI
import kotlin.math.sin

/**
 * The mark, and the state machine, which are the same object.
 *
 * > "The mark *is* the state. The dashed ring's rotation period and the core
 * > dot's diameter are bound to V's state. Implement it as one component that
 * > takes a state, not as a static asset plus separate animations."
 *
 * So there is no drawable and no Lottie file here: one canvas, whose geometry
 * never changes and whose motion is read off `state`. The device, the app icon
 * and this are the same object at different sizes — `res/drawable/
 * ic_aperture_fg.xml` is the same three circles with the dash dropped, per the
 * 16px reduction rule.
 *
 * ### Reactor
 *
 * This is direction **2b/2d** of the *V Mobile* canvas, chosen over *Core
 * Field* and *Bloom*. The three circles underneath are untouched — same
 * geometry, same tokens, same two curves. What Reactor adds is depth and
 * instrument density, and each layer is here to answer something the flat mark
 * could not:
 *
 * - **Two rings of machined ticks** (120 fine, 24 coarse, counter-rotating at
 *   90s and 60s) give the field a fixed frame of reference. Without them a
 *   slow rotation is invisible; against them it is obvious.
 * - **Two specular sweeps** turning at 5.5s and 8.5s give the rotation a
 *   *direction*. A uniform ring reads as a flat circle with nothing happening
 *   no matter how fast it spins.
 * - **An inner haze and a glass shell** put the core behind something, so the
 *   core dot reads as lit rather than drawn.
 * - **Dust** keeps the field alive between states, which is what stops IDLE
 *   from looking like a crash.
 *
 * Everything here is light and shading — there is no geometry that a still
 * frame would show. The whole composition scales with [stage], because the
 * proportions are the design and a phone header is not a 340dp square.
 */
enum class Ambient { IDLE, WAKE, LISTENING, THINKING, SPEAKING, MUTED }

/**
 * The per-state numbers, quoted from the canvas's state table.
 *
 * Sizes are fractions of the stage rather than pixels, because the canvas
 * specifies them against a 340px stage and this is drawn into anything from a
 * 132dp chat header to a 300dp ambient screen. A fraction survives that; a
 * pixel does not.
 */
private data class Spec(
    val ap: Float,              // aperture diameter
    val core: Float,            // core dot diameter
    val glow: Float,            // drives every lit layer's opacity
    val halo: Float,            // halo alpha
    val breatheMs: Int,         // core dot pulse; 0 = none
    val spinSeconds: Float,     // dashed ring rotation period; 0 = still
    val ringAlpha: Float,       // outer solid ring
    val dashAlpha: Float,       // dashed ring
    val gyro: Float,            // thinking gyroscope opacity
    val emit: Boolean,          // the expanding ring — he is putting out sound
    val live: Boolean,          // false only when nothing at all is running
)

private const val REF = 340f    // the canvas's stage, in its own pixels

/** A length quoted from the canvas in its own pixels, as a fraction of stage. */
private fun px(v: Float) = v / REF

private fun spec(state: Ambient) = when (state) {
    Ambient.IDLE      -> Spec(px(172f), px(30f), .22f, .14f, 5000, 26.0f, 1f, 1f, 0f, false, true)
    Ambient.WAKE      -> Spec(px(172f), px(38f), .55f, .40f, 1400, 3.4f, 1f, 1f, 0f, true, true)
    Ambient.LISTENING -> Spec(px(172f), px(34f), .60f, .42f, 2200, 6.0f, 1f, 1f, 0f, false, true)
    // The aperture shrinks to 104 so the gyroscope reads as bigger than him,
    // and the rings fade back — while he is thinking, the tools are the
    // subject and he is not.
    Ambient.THINKING  -> Spec(px(104f), px(18f), .42f, .30f, 2200, 0f, .28f, .2f, 1f, false, true)
    Ambient.SPEAKING  -> Spec(px(172f), px(48f), .70f, .50f, 1100, 2.2f, 1f, 1f, 0f, true, true)
    // A state no software path can produce has to look like one: flat unlit
    // grey, zero glow, zero animation. Nothing else in the system is still,
    // and `glow` at zero takes every Reactor layer down with it — the machined
    // rings and the sweeps are not merely dimmed here, they are gone.
    Ambient.MUTED     -> Spec(px(172f), px(25f), 0f, 0f, 0, 0f, 1f, 1f, 0f, false, false)
}

@Composable
fun Aperture(
    state: Ambient,
    modifier: Modifier = Modifier,
    /** One gyroscope ring per tool in flight. A one-step answer spins one
     *  ring; a four-tool answer visibly spins more. Never three for a lookup. */
    activeTools: Int = 1,
    reducedMotion: Boolean = false,
    /** The square the whole composition is drawn into. Scaling it scales
     *  everything in it by the same ratio, so the proportions the mark is
     *  specified in survive being put in a phone's header. */
    stage: Dp = 300.dp,
) {
    val s = spec(state)
    val m = motion(s, live = s.live && !reducedMotion)

    // Size is information and survives reduced motion; only the loops stop.
    val ap by token(s.ap, 550, "aperture-size")
    val core by token(s.core, V.STATE_MS, "core-size")
    val glow by token(s.glow, V.STATE_MS, "glow")
    val halo by token(s.halo, V.STATE_MS, "halo")
    val ringA by token(s.ringAlpha, V.STATE_MS, "ring-alpha")
    val dashA by token(s.dashAlpha, V.STATE_MS, "dash-alpha")
    val gyroA by token(s.gyro, 320, "gyro-alpha")

    val muted = state == Ambient.MUTED
    val rings = activeTools.coerceIn(1, 3)

    Canvas(modifier.size(stage)) {
        val d = size.minDimension

        drawHalo(d, halo, m.halo)
        drawMachinedTicks(d, glow, m.fineTicks, m.coarseTicks)
        drawSpecular(d, glow, m.specularA, m.specularB)
        drawHaze(d, glow)
        drawShell(d, glow)
        if (gyroA > 0.004f) drawGyroscope(d, gyroA, rings, m.gyro1, m.gyro2, m.gyro3)
        if (glow > 0f) drawDust(d, glow, m.dust, m.dustPulse)
        drawAperture(d, ap, core, glow, ringA, dashA, m.dash, m.breath, muted)
        if (m.emit >= 0f) drawEmit(d, m.emit)
    }
}

// ---- motion ---------------------------------------------------------------

/**
 * Every loop in the composition, resolved once.
 *
 * Gathered into one object rather than declared at the call site so that the
 * whole set can be switched off in a single branch. A muted mark that is still
 * running eleven infinite transitions and throwing their values away would
 * redraw every frame to show a static picture.
 */
private class Motion(
    val halo: Float, val fineTicks: Float, val coarseTicks: Float,
    val specularA: Float, val specularB: Float,
    val gyro1: Float, val gyro2: Float, val gyro3: Float,
    val dust: Float, val dustPulse: Float,
    val dash: Float, val breath: Float,
    /** 0..1 through the emit ring's expansion, or -1 when it is not emitting. */
    val emit: Float,
)

@Composable
private fun motion(s: Spec, live: Boolean): Motion {
    if (!live) return Motion(1f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 1f, -1f)

    val loop = rememberInfiniteTransition(label = "reactor")

    // The frame of reference: slow enough to be a background, fast enough that
    // the eye can tell it from a still image.
    val fine by loop.turn(90_000, "fine")
    val coarse by loop.turn(60_000, "coarse")
    // The sweeps. These are what make the rotation legible as rotation.
    val specA by loop.turn(5_500, "spec-a")
    val specB by loop.turn(8_500, "spec-b")
    val dust by loop.turn(26_000, "dust")
    val gyro1 by loop.turn(2_600, "gyro-1")
    val gyro2 by loop.turn(3_900, "gyro-2")
    val gyro3 by loop.turn(6_200, "gyro-3")

    val halo by loop.animateFloat(
        1f, 1.18f,
        infiniteRepeatable(tween(2500, easing = Breathe), RepeatMode.Reverse),
        label = "halo")

    val dustPulse by loop.animateFloat(
        0f, 1f,
        infiniteRepeatable(tween(3100, easing = Breathe), RepeatMode.Reverse),
        label = "dust-pulse")

    val dash by if (s.spinSeconds > 0f) {
        loop.turn((s.spinSeconds * 1000).toInt(), "dash")
    } else rememberStatic(0f)

    val breath by if (s.breatheMs > 0) {
        loop.animateFloat(
            1f, 1.045f,
            infiniteRepeatable(tween(s.breatheMs / 2, easing = Breathe), RepeatMode.Reverse),
            label = "breathe")
    } else rememberStatic(1f)

    val emit by if (s.emit) {
        loop.animateFloat(
            0f, 1f,
            infiniteRepeatable(tween(2200, easing = Arrive)),
            label = "emit")
    } else rememberStatic(-1f)

    return Motion(halo, fine, coarse, specA, specB, gyro1, gyro2, gyro3,
                  dust, dustPulse, dash, breath, emit)
}

/** One full turn every [periodMs]. Negative to turn the other way. */
@Composable
private fun InfiniteTransition.turn(periodMs: Int, label: String) =
    animateFloat(
        0f, 360f,
        infiniteRepeatable(tween(periodMs, easing = LinearEasing)),
        label = label)

@Composable
private fun token(target: Float, ms: Int, label: String) =
    androidx.compose.animation.core.animateFloatAsState(
        targetValue = target, animationSpec = tween(ms, easing = Arrive), label = label)

// ---- the layers, outermost first -----------------------------------------

/** The breath of the whole field. Present in every state but muted. */
private fun DrawScope.drawHalo(d: Float, alpha: Float, pulse: Float) {
    if (alpha <= 0f) return
    val r = d * px(300f) / 2f * pulse
    drawCircle(
        brush = Brush.radialGradient(
            0f to V.seed.copy(alpha = alpha),
            .66f to Color.Transparent,
            1f to Color.Transparent,
            center = center, radius = r),
        radius = r, center = center)
}

/**
 * Two counter-rotating rings of graduated ticks.
 *
 * Drawn as dashed circles rather than as 144 separate marks: the dash lengths
 * are computed from the tick's *angle*, so the pattern closes exactly on the
 * circumference at any stage size and there is never a visible seam where the
 * last tick meets the first.
 */
private fun DrawScope.drawMachinedTicks(d: Float, glow: Float, fine: Float, coarse: Float) {
    if (glow <= 0f) return

    val fineR = d * px(164.1f)
    rotate(fine) {
        drawCircle(
            color = V.seed.copy(alpha = .55f * glow * 1.15f),
            radius = fineR, center = center,
            style = Stroke(width = d * px(5.85f),
                           pathEffect = ticks(fineR, onDeg = .34f, periodDeg = 3f)))
    }

    val coarseR = d * px(139.9f)
    rotate(-coarse) {
        drawCircle(
            color = V.lift.copy(alpha = .7f * glow),
            radius = coarseR, center = center,
            style = Stroke(width = d * px(10.15f),
                           pathEffect = ticks(coarseR, onDeg = 1.1f, periodDeg = 15f)))
    }
}

/**
 * The two specular arcs — a bright leading edge that fades back into nothing.
 *
 * Each is drawn twice: once wide and faint for the bloom the CSS gets from a
 * drop-shadow, then once crisp on top. Without the bloom the arc reads as a
 * drawn stroke rather than as light catching a machined edge.
 */
private fun DrawScope.drawSpecular(d: Float, glow: Float, a: Float, b: Float) {
    if (glow <= 0f) return

    val rA = d * px(153.1f)
    val brushA = Brush.sweepGradient(
        0f to Color.Transparent,
        250f / 360f to Color.Transparent,
        285f / 360f to V.lift.copy(alpha = .05f),
        352f / 360f to V.coreHi.copy(alpha = .95f),
        1f to Color.Transparent,
        center = center)
    // -90 because a sweep gradient starts at three o'clock and the canvas
    // quotes its angles from twelve.
    rotate(a - 90f) {
        drawCircle(brushA, rA, center, alpha = glow * .55f,
                   style = Stroke(width = d * px(22f)))
        drawCircle(brushA, rA, center, alpha = glow * 1.2f,
                   style = Stroke(width = d * px(7.85f)))
    }

    val rB = d * px(129.4f)
    val brushB = Brush.sweepGradient(
        0f to Color.Transparent,
        300f / 360f to Color.Transparent,
        358f / 360f to V.seed.copy(alpha = .9f),
        1f to Color.Transparent,
        center = center)
    rotate(-b + 90f) {
        drawCircle(brushB, rB, center, alpha = glow * .95f,
                   style = Stroke(width = d * px(3.3f)))
    }
}

/** The haze that puts the core behind glass. Lit from below, like the core. */
private fun DrawScope.drawHaze(d: Float, glow: Float) {
    if (glow <= 0f) return
    val r = d * px(232f) / 2f
    drawCircle(
        brush = Brush.radialGradient(
            0f to V.seed.copy(alpha = .30f),
            .52f to V.deep.copy(alpha = .08f),
            .74f to Color.Transparent,
            1f to Color.Transparent,
            center = center + Offset(0f, d * px(18.6f)), radius = r),
        radius = r, center = center, alpha = (glow * 1.4f).coerceAtMost(1f))
}

/** The shell: one hairline, and the inner glow that makes it a surface. */
private fun DrawScope.drawShell(d: Float, glow: Float) {
    if (glow <= 0f) return
    val r = d * px(210f) / 2f
    val a = (glow * 1.4f).coerceAtMost(1f)
    drawCircle(
        brush = Brush.radialGradient(
            0f to Color.Transparent,
            .56f to Color.Transparent,
            1f to V.seed.copy(alpha = .18f),
            center = center, radius = r),
        radius = r, center = center, alpha = a)
    drawCircle(color = V.seed.copy(alpha = .16f), radius = r, center = center,
               alpha = a, style = Stroke(width = d * px(1f)))
}

/**
 * The thinking gyroscope — one ring per tool in flight.
 *
 * Each ring is squashed vertically and rotated at the same time, which is the
 * whole trick: a circle that only rotates reads as flat, and a circle that only
 * squashes reads as a wobble. Doing both, out of phase, reads as a ring on an
 * axis. The single bright arc on the first two is the specular that says which
 * way round it is going.
 */
private fun DrawScope.drawGyroscope(
    d: Float, alpha: Float, rings: Int, t1: Float, t2: Float, t3: Float,
) {
    // vSquash: a full turn per cycle, with the squash peaking at the halfway
    // point — so the ring is flattest edge-on and open when it faces us.
    fun squash(turnDeg: Float, lo: Float, hi: Float) =
        lo + (hi - lo) * sin(PI * (turnDeg / 360f)).toFloat()

    if (rings >= 1) {
        val r = d * px(121.8f)
        rotate(t1) {
            scale(1f, squash(t1, .24f, .42f)) {
                drawCircle(
                    brush = Brush.sweepGradient(
                        0f to V.seed.copy(alpha = .12f),
                        200f / 360f to V.seed.copy(alpha = .12f),
                        300f / 360f to V.coreHi.copy(alpha = .9f),
                        1f to V.seed.copy(alpha = .12f),
                        center = center),
                    radius = r, center = center, alpha = alpha,
                    style = Stroke(width = d * px(4.34f)))
            }
        }
    }
    if (rings >= 2) {
        val r = d * px(100.2f)
        rotate(-t2) {
            scale(1f, squash(t2, .5f, .2f)) {
                drawCircle(
                    brush = Brush.sweepGradient(
                        0f to V.seed.copy(alpha = .1f),
                        210f / 360f to V.seed.copy(alpha = .1f),
                        310f / 360f to V.lift.copy(alpha = .85f),
                        1f to V.seed.copy(alpha = .1f),
                        center = center),
                    radius = r, center = center, alpha = alpha,
                    style = Stroke(width = d * px(3.57f)))
            }
        }
    }
    if (rings >= 3) {
        // The innermost is graduated rather than swept — three identical
        // specular rings would read as one thick ring.
        val r = d * px(78.6f)
        rotate(t3) {
            drawCircle(
                color = V.seed.copy(alpha = .5f), radius = r, center = center,
                alpha = alpha,
                style = Stroke(width = d * px(2.8f),
                               pathEffect = ticks(r, onDeg = 2f, periodDeg = 8f)))
        }
    }
}

/**
 * Where the three motes sit in the orbiting box, as fractions of its half-width,
 * with the phase offset that keeps them from blinking in unison.
 *
 * Hoisted out of the draw call because this runs on every frame of every state:
 * three `Triple`s allocated sixty times a second is garbage a mark this small
 * has no business making.
 */
private val MOTES = listOf(
    Triple(Offset(0f, -1f), V.coreHi, 0f),
    Triple(Offset(.88f, .64f), V.lift, .35f),
    Triple(Offset(-.84f, -.40f), V.seed, .7f),
)

/** Dust. Not information — it is what keeps IDLE from looking like a freeze. */
private fun DrawScope.drawDust(d: Float, glow: Float, turn: Float, pulse: Float) {
    val half = d * px(198f) / 2f
    rotate(turn) {
        for ((at, colour, phase) in MOTES) {
            // Each mote pulses on its own phase, so the three never blink
            // together and the field never looks like it is being clocked.
            val p = ((pulse + phase) % 1f).let { if (it > .5f) 1f - it else it } * 2f
            drawCircle(
                color = colour.copy(alpha = ((.2f + .8f * p) * glow * 1.6f).coerceAtMost(1f)),
                radius = d * px(2f) / 2f * (.7f + .65f * p),
                center = center + Offset(at.x * half, at.y * half))
        }
    }
}

/** The three circles the whole system is built on. Unchanged since the brief. */
private fun DrawScope.drawAperture(
    d: Float, apFrac: Float, coreFrac: Float, glow: Float,
    ringAlpha: Float, dashAlpha: Float, dashTurn: Float, breath: Float,
    muted: Boolean,
) {
    val ap = d * apFrac
    // 2.5px at any aperture size: the ring does not get thinner because he is
    // thinking, it gets smaller.
    val stroke = d * px(2.5f)
    val ringC = if (muted) V.line3 else V.deep
    val dashC = if (muted) V.line3 else V.seed

    if (glow > 0f) {
        val bloom = ap / 2f + d * px(40f) * glow
        drawCircle(
            brush = Brush.radialGradient(
                0f to V.seed.copy(alpha = glow * .5f),
                .5f to V.seed.copy(alpha = glow * .35f),
                1f to Color.Transparent,
                center = center, radius = bloom),
            radius = bloom, center = center)
    }

    drawCircle(color = ringC.copy(alpha = ringAlpha), radius = (ap - stroke) / 2f,
               center = center, style = Stroke(width = stroke))
    // The rim highlight — the ring is lit from above, so it is not one flat
    // colour all the way round.
    if (!muted) {
        drawCircle(
            brush = Brush.verticalGradient(
                0f to V.coreHi.copy(alpha = .18f),
                .45f to Color.Transparent,
                1f to Color.Transparent,
                startY = center.y - ap / 2f, endY = center.y + ap / 2f),
            radius = (ap - stroke) / 2f, center = center,
            alpha = ringAlpha, style = Stroke(width = stroke))
    }

    rotate(dashTurn) {
        drawCircle(
            color = dashC.copy(alpha = dashAlpha), radius = ap * .33f, center = center,
            style = Stroke(width = stroke,
                           pathEffect = PathEffect.dashPathEffect(
                               floatArrayOf(ap * .055f, ap * .045f))))
    }

    drawCore(d, d * coreFrac / 2f * breath, glow, muted)
}

private fun DrawScope.drawCore(d: Float, radius: Float, glow: Float, muted: Boolean) {
    if (muted) {
        drawCircle(color = V.line2, radius = radius, center = center)
        return
    }
    if (glow > 0f) {
        val bloom = radius + d * px(22f)
        drawCircle(
            brush = Brush.radialGradient(
                0f to V.seed.copy(alpha = .65f * glow),
                1f to Color.Transparent,
                center = center, radius = bloom),
            radius = bloom, center = center)
    }
    // The highlight sits at 34%/30%, not at the centre: a dot lit from one side
    // reads as a sphere, and a centred gradient reads as a flat disc with a
    // bright middle.
    val lit = center - Offset(radius * .32f, radius * .40f)
    drawCircle(
        brush = Brush.radialGradient(
            0f to V.coreHi, .62f to Color(0xFF8E75FF), 1f to V.deep,
            center = lit, radius = radius * 1.6f),
        radius = radius, center = center)
    // …and the terminator opposite it. Reactor's core is a sphere in a housing,
    // not a disc: without the far rim going dark it floats off the ring.
    drawCircle(
        brush = Brush.radialGradient(
            0f to Color.Transparent,
            .62f to Color.Transparent,
            1f to Color(0xFF2B1A6E).copy(alpha = .55f),
            center = lit, radius = radius * 1.05f),
        radius = radius, center = center)
}

/**
 * The ring that leaves.
 *
 * Only two states emit: WAKE and SPEAKING — the two where sound is coming *out*
 * of the device. It is the one piece of the composition that is not a loop
 * around the centre but a thing departing it, which is why it is legible
 * across a room as "he is talking" rather than "he is on".
 */
private fun DrawScope.drawEmit(d: Float, t: Float) {
    val r = d * px(322f) / 2f * (.82f + .93f * t)
    drawCircle(
        color = V.coreHi.copy(alpha = .35f * .65f * (1f - t)),
        radius = r, center = center, style = Stroke(width = d * px(1f)))
}

/**
 * A dash pattern of [onDeg]-wide ticks repeating every [periodDeg].
 *
 * Specified in degrees because that is what closes: 360 divided by the period
 * has to be a whole number of ticks or the ring has a seam, and that stays true
 * at every radius while a pixel length does not.
 */
private fun ticks(radius: Float, onDeg: Float, periodDeg: Float): PathEffect {
    val perDeg = radius * (PI / 180.0).toFloat()
    val on = onDeg * perDeg
    return PathEffect.dashPathEffect(floatArrayOf(on, periodDeg * perDeg - on))
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
