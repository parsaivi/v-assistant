package cloud.brain.v

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.provideContent
import androidx.glance.action.actionStartActivity
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextDecoration
import java.time.LocalDateTime

/**
 * The day, with the next thing on it as the headline.
 *
 * These started as two ideas — a card for today and a one-line card for the
 * next commitment — and they are better as one, because the second is the only
 * part of the first anyone reads at a glance. So what is left of today is the
 * hero and the rest is small print under it, rather than a list of five equal
 * rows in which the one that matters has to be found.
 *
 * **The next thing is not automatically the hero.** `calendar.next_up` looks
 * two weeks ahead, and the first version of this card simply drew whatever it
 * returned — so on an evening when today's two meetings were already over, a
 * card titled TODAY had tomorrow's all-day item as its headline and nothing
 * about today on it at all. The rule now: the hero is today's, or there is no
 * hero. What is coming later still appears, under its own label, saying which
 * day it belongs to. A card that has to be read twice to find out whether it
 * is talking about now is worse than one that admits the day is over.
 */
class TodayWidget : GlanceAppWidget() {

    override val sizeMode = SizeMode.Exact

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val snap = Snapshot.load(context)
        provideContent { Card(snap) }
    }

    @Composable
    private fun Card(snap: Snapshot.Glance) {
        val now = LocalDateTime.now()
        // `past` is decided on the server against its own clock, and that is
        // worth taking on trust: the laptop is where the calendar is, and its
        // idea of "already happened" is the one the 10:00 brief used.
        val today = snap.events.filter { it.today }
        val ahead = today.filter { !it.past }
        val next = snap.next

        Column(modifier = W.card().fillMaxSize()
            .clickable(actionStartActivity<MainActivity>())) {

            W.Header("TODAY", V.VOICE, snap,
                     right = if (snap.everFetched) snap.headline else "",
                     onRefresh = actionRunCallback<RefreshWidgets>())

            // One child of the root, whichever branch runs — see `W.Header`
            // for why every section on these cards is nested.
            Column(modifier = GlanceModifier.fillMaxWidth()) {
                when {
                    !snap.everFetched ->
                        Text("Open V once to pair this card.",
                             style = W.bodyStyle(W.muted))

                    ahead.isNotEmpty() -> {
                        Hero(ahead.first(), now)
                        if (ahead.size > 1) {
                            Spacer(GlanceModifier.height(8.dp))
                            ahead.drop(1).take(2).forEach { Later(it, over = false) }
                        }
                    }

                    else -> {
                        // The day is spent. Said plainly, and then shown: the
                        // meetings that already happened are what makes this a
                        // card about today rather than a blank rectangle with
                        // a heading on it.
                        Text(if (today.isEmpty()) "Nothing on the calendar today."
                             else "Nothing left today.",
                             style = W.bodyStyle(W.muted))
                        if (today.isNotEmpty()) {
                            Spacer(GlanceModifier.height(8.dp))
                            today.take(3).forEach { Later(it, over = true) }
                        }
                    }
                }
            }

            // What is coming, when it is not today — under its own label and
            // naming its day, so it can never be mistaken for the headline.
            if (next != null && !next.today) {
                Spacer(GlanceModifier.height(12.dp))
                NextUp(next)
            }

            Spacer(GlanceModifier.defaultWeight())
            Footer(snap)
        }
    }

    /** The next commitment, when it belongs to another day. One child. */
    @Composable
    private fun NextUp(ev: Snapshot.Ev) {
        Column(modifier = GlanceModifier.fillMaxWidth()) {
            Text("NEXT · ${Snapshot.dayLabel(ev.date).ifBlank { "later" }}",
                 style = W.labelStyle())
            Spacer(GlanceModifier.height(4.dp))
            Row(modifier = GlanceModifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically) {
                Text(if (ev.allDay) "all-day" else ev.when_,
                     style = W.metaStyle(W.muted))
                Spacer(GlanceModifier.width(9.dp))
                Text(ev.what, maxLines = 2, style = W.bodyStyle(W.bodyColor),
                     modifier = GlanceModifier.defaultWeight())
            }
        }
    }

    /** The next thing: its time, what it is, and how long you have. */
    @Composable
    private fun Hero(ev: Snapshot.Ev, now: LocalDateTime) = Column(
        modifier = GlanceModifier.fillMaxWidth()) {
        Row(modifier = GlanceModifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically) {
            Text(if (ev.allDay) "all-day" else ev.when_,
                 style = W.metaStyle(W.seed).copy(fontSize = W.time,
                                                  fontWeight = FontWeight.Medium))
            Spacer(GlanceModifier.width(10.dp))
            Text(ev.what, maxLines = 2,
                 style = W.bodyStyle(W.paperHi, FontWeight.Medium)
                     .copy(fontSize = W.hero),
                 modifier = GlanceModifier.defaultWeight())
            Spacer(GlanceModifier.width(8.dp))
            // Computed here, at draw time, from the instant the server sent.
            // See `Snapshot.gap` for why this is not the server's job — and
            // `dayLabel` for why "not today" is not the same as "tomorrow".
            Text(if (ev.today) Snapshot.gap(ev.at, now)
                 else Snapshot.dayLabel(ev.date),
                 style = W.metaStyle(W.dim), maxLines = 1)
        }
        if (ev.where.isNotBlank()) {
            Spacer(GlanceModifier.height(2.dp))
            Text(ev.where, style = W.metaStyle(W.muted), maxLines = 1)
        }
    }

    /** One more thing today. [over] dims it further and strikes it through —
     *  a meeting that already happened is still part of the day, but it is not
     *  something to act on, and the two must not look alike. */
    @Composable
    private fun Later(ev: Snapshot.Ev, over: Boolean) {
        Row(modifier = GlanceModifier.fillMaxWidth().padding(vertical = 1.dp)) {
            // A plain middle dot, deliberately not one of the brief's six
            // glyphs: `●` means "local, nothing left the machine", and
            // spending it as a bullet would be the icon set saying nothing.
            Text("· ${if (ev.allDay) "all-day" else ev.when_}",
                 style = W.metaStyle(if (over) W.dim3 else W.dim))
            Spacer(GlanceModifier.width(8.dp))
            Text(ev.what, maxLines = 1,
                 style = W.bodyStyle(if (over) W.dim else W.bodyColor).copy(
                     textDecoration = if (over) TextDecoration.LineThrough
                                      else TextDecoration.None),
                 modifier = GlanceModifier.defaultWeight())
        }
    }

    /**
     * The counts, in the same grammar as the cost line: glyph, number, noun,
     * separated by `·`, dim and never hidden. Overdue is the only thing that
     * gets a colour, because it is the only one that is news.
     */
    @Composable
    private fun Footer(snap: Snapshot.Glance) {
        val c = snap.counts
        Row(modifier = GlanceModifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically) {
            Text("${V.NOTE} ${c.open} open", style = W.metaStyle())
            if (c.overdue > 0) {
                Text(" · ${c.overdue} overdue", style = W.metaStyle(W.unsure))
            }
            if (c.reminders > 0) {
                Text(" · ${c.reminders} reminder", style = W.metaStyle())
            }
            Spacer(GlanceModifier.defaultWeight())
            if (c.closedLast7 > 0) {
                Text("${c.closedLast7} done this week", style = W.metaStyle(W.free))
            }
        }
    }
}
