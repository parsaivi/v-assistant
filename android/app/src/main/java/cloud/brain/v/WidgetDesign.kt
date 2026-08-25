package cloud.brain.v

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceModifier
import androidx.glance.ImageProvider
import androidx.glance.action.clickable
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontFamily
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import androidx.compose.runtime.Composable

/**
 * The design, as much of it as a widget is allowed to have.
 *
 * Three things the brief specifies cannot cross into a home screen, and they
 * are worth naming rather than quietly dropping:
 *
 *  1. **IBM Plex is gone.** A widget is `RemoteViews` in the launcher's
 *     process, which can only name font families the platform already has, so
 *     the mono is the system mono. Everything the brief sets in Plex Mono —
 *     metadata, numbers, the glyphs — is set in `FontFamily.Monospace` here so
 *     the rhythm survives even though the face does not.
 *  2. **Nothing animates.** There is no aperture on these cards for that
 *     reason: the mark *is* the state machine, and a still picture of it would
 *     be the one state the brief says must look unlit and unmoving. `✦` in the
 *     header is the voice glyph, which was always allowed to be static.
 *  3. **There is no error state.** A widget cannot show a failed request
 *     without becoming a bug report on someone's wallpaper, so a card that
 *     could not reach V shows the last true thing with its age in amber. Same
 *     honesty the cost line is built on, different surface.
 */
object W {

    val label = 9.5.sp
    val meta = 11.sp
    val body = 13.sp
    val hero = 17.sp
    val time = 15.sp

    fun c(color: Color) = ColorProvider(color)

    val dim = c(V.dim2)
    val dim3 = c(V.dim3)
    val section = c(V.dim1)
    val paper = c(V.paper)
    val paperHi = c(V.paperHi)
    val muted = c(V.muted)
    val bodyColor = c(V.body)
    val seed = c(V.seed)
    val unsure = c(V.unsure)
    val free = c(V.free)

    /** The card itself. A drawable rather than `GlanceModifier.cornerRadius`,
     *  which only rounds from API 31 — this app runs from 26, and a square
     *  black slab on a wallpaper looks like a rendering failure. */
    fun card() = GlanceModifier
        .fillMaxWidth()
        .background(ImageProvider(R.drawable.widget_card))
        .padding(horizontal = 14.dp, vertical = 12.dp)

    fun labelStyle(color: ColorProvider = section) = TextStyle(
        color = color, fontSize = label, fontFamily = FontFamily.Monospace,
        fontWeight = FontWeight.Medium)

    fun metaStyle(color: ColorProvider = dim) = TextStyle(
        color = color, fontSize = meta, fontFamily = FontFamily.Monospace)

    fun bodyStyle(color: ColorProvider = paper, weight: FontWeight = FontWeight.Normal) =
        TextStyle(color = color, fontSize = body, fontWeight = weight)

    /**
     * `✦ TODAY ───── 09:12`, and the tap target that refreshes the card.
     *
     * The right-hand side is the age of what is being shown, always present
     * and never hidden — a card that cannot say when it was last true is a
     * card that quietly lies every time the laptop sleeps. Amber past
     * half an hour, which is also the floor Android puts on a widget's own
     * update period, so anything fresher than that is simply "current".
     */
    @Composable
    fun Header(title: String, glyph: String, snap: Snapshot.Glance,
               right: String, onRefresh: androidx.glance.action.Action) {
        // Wrapped in its own Column so it costs the caller *one* child and not
        // two. Glance compiles to RemoteViews, where a Column can hold at most
        // ten children and the eleventh is not clipped but fatal — the whole
        // translation throws and the widget is left showing the loading
        // layout, which looks exactly like a card that never loaded. Every
        // section here is nested for that reason.
        Column(modifier = GlanceModifier.fillMaxWidth()) {
            Row(modifier = GlanceModifier.fillMaxWidth().clickable(onRefresh),
                verticalAlignment = Alignment.CenterVertically) {
                Text("$glyph $title", style = labelStyle())
                Spacer(GlanceModifier.width(10.dp))
                Text(right, style = metaStyle(), maxLines = 1,
                     modifier = GlanceModifier.defaultWeight())
                if (snap.everFetched) {
                    Text(snap.age(), style = metaStyle(
                        if (snap.stale()) unsure else dim))
                } else {
                    Text("not reached", style = metaStyle(unsure))
                }
            }
            Spacer(GlanceModifier.height(8.dp))
        }
    }
}
