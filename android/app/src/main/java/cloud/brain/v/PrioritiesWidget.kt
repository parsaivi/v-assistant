package cloud.brain.v

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.provideContent
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

/**
 * What to do next, and a box to tick when it is done.
 *
 * The tick is the whole reason this card exists rather than being a list. A
 * read-only widget is a thing you glance at and then open the app anyway; the
 * one that changes behaviour is the one where finishing something takes one
 * tap on the home screen and the checkbox in the Markdown note it came from
 * changes as a `[bot]` commit that `v undo` reverts. Nothing about that path
 * is new — `engine.act("complete_task")` is the same call the chat buttons
 * make — the widget just presses it from further away.
 *
 * Ticking is optimistic. The box strikes through the moment it is pressed and
 * the request goes out behind it, because a home screen that waits two seconds
 * for a laptop to answer before acknowledging a tap feels broken even when it
 * is working. If the write fails the next fetch puts the task back, which is
 * the correct outcome and the one the file format guarantees: the note is the
 * truth, and this card is a picture of it.
 */
class PrioritiesWidget : GlanceAppWidget() {

    override val sizeMode = SizeMode.Exact

    companion object {
        /** Height of one row plus its padding, measured rather than guessed —
         *  it is what decides how many fit, and a row half-drawn at the bottom
         *  of the card is worse than one row fewer. */
        private const val ROW_DP = 30
        private const val CHROME_DP = 62      // header + footer + card padding
    }

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val snap = Snapshot.load(context)
        provideContent { Card(snap) }
    }

    @Composable
    private fun Card(snap: Snapshot.Glance) {
        val fits = ((LocalSize.current.height.value - CHROME_DP) / ROW_DP)
            .toInt().coerceIn(1, 5)
        val rows = snap.tasksLeft().take(fits)

        Column(modifier = W.card().fillMaxSize()) {
            W.Header("PRIORITIES", V.NOTE, snap,
                     right = overdueLabel(snap),
                     onRefresh = actionRunCallback<RefreshWidgets>())

            if (rows.isEmpty()) {
                Text(when {
                        !snap.everFetched -> "Open V once to pair this card."
                        snap.tasks.isEmpty() -> "Nothing open. The vault is clear."
                        else -> "All caught up."
                     },
                     style = W.bodyStyle(W.muted),
                     modifier = GlanceModifier.clickable(
                         actionStartActivity<MainActivity>()))
            } else {
                // Nested: five rows plus a ticked one plus the header, footer
                // and spacer would sit right on Glance's ten-child ceiling for
                // a Column, and the eleventh child does not clip — it throws,
                // and the card is left showing the loading layout forever.
                Column(modifier = GlanceModifier.fillMaxWidth()) {
                    rows.forEach { TaskRow(it, done = false) }
                    // The ones ticked a second ago, still struck through, so
                    // the list does not jump under a thumb still resting on it.
                    snap.tasks.filter { it.id in snap.done }.take(1)
                        .forEach { TaskRow(it, done = true) }
                }
            }

            Spacer(GlanceModifier.defaultWeight())
            Footer(snap)
        }
    }

    private fun overdueLabel(snap: Snapshot.Glance): String {
        val c = snap.counts
        return when {
            !snap.everFetched -> ""
            c.overdue > 0 -> "${c.overdue} overdue"
            c.dueToday > 0 -> "${c.dueToday} due today"
            else -> ""
        }
    }

    /**
     * One task: a box, the text, and when it was due.
     *
     * The box and the text are separate tap targets on purpose. The box
     * completes; the text opens the app at the conversation, because a task
     * you cannot finish in one tap is usually one you need the note for. Two
     * destinations one thumb-width apart is a real risk, which is why the box
     * carries the padding rather than the row.
     */
    @Composable
    private fun TaskRow(item: Snapshot.Item, done: Boolean) {
        Row(modifier = GlanceModifier.fillMaxWidth().height(ROW_DP.dp),
            verticalAlignment = Alignment.CenterVertically) {

            Text(if (done) "☑" else if (item.doing) "◐" else "☐",
                 style = W.metaStyle(if (done) W.free else W.dim)
                     .copy(fontSize = W.body),
                 modifier = GlanceModifier
                     .padding(end = 10.dp, top = 4.dp, bottom = 4.dp)
                     .clickable(
                         if (done) actionStartActivity<MainActivity>()
                         else actionRunCallback<CompleteTask>(
                             actionParametersOf(CompleteTask.TASK to item.id))))

            Text(item.text, maxLines = 1,
                 style = W.bodyStyle(if (done) W.dim else W.paper).copy(
                     textDecoration = if (done) TextDecoration.LineThrough
                                      else TextDecoration.None),
                 modifier = GlanceModifier.defaultWeight()
                     .clickable(actionStartActivity<MainActivity>()))

            val due = Snapshot.dueLabel(item)
            if (due.isNotBlank() && !done) {
                Spacer(GlanceModifier.width(8.dp))
                Text(due, maxLines = 1,
                     style = W.metaStyle(if (item.overdue) W.unsure else W.dim))
            }
        }
    }

    @Composable
    private fun Footer(snap: Snapshot.Glance) {
        val c = snap.counts
        Row(modifier = GlanceModifier.fillMaxWidth()) {
            Text("${V.NOTE} ${c.open} open", style = W.metaStyle())
            Spacer(GlanceModifier.defaultWeight())
            if (c.closedLast7 > 0) {
                Text("${c.closedLast7} done this week", style = W.metaStyle(W.free))
            }
        }
    }
}

/** The header tap. One fetch, both cards. */
class RefreshWidgets : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId,
                                  parameters: ActionParameters) {
        Widgets.refreshNow(context.applicationContext, force = true)
    }
}

/**
 * A box, ticked from the home screen.
 *
 * The order matters and is the opposite of the obvious one: the local mark
 * goes down and the card redraws *before* the request is made, so the tap is
 * acknowledged in the time it takes to redraw rather than in the time it takes
 * to complete a TLS handshake with a sleeping laptop. The write is still a
 * write — `[bot]`-committed, undoable — it is just no longer what the thumb is
 * waiting for.
 */
class CompleteTask : ActionCallback {

    companion object {
        val TASK = ActionParameters.Key<Long>("task")
    }

    override suspend fun onAction(context: Context, glanceId: GlanceId,
                                  parameters: ActionParameters) {
        val id = parameters[TASK] ?: return
        val app = context.applicationContext
        Snapshot.markDone(app, id)
        Widgets.redraw(app)
        try {
            Api(app).act(Button("done", "complete_task", """{"id":$id}"""))
        } catch (e: Exception) {
            // The tick stays local until the next fetch disagrees with it,
            // which is the honest outcome: the note is the truth and this is
            // a picture of it. Nothing to show here — a widget has no room
            // for a failure and no user watching it at this moment.
        }
        Widgets.refreshNow(app, force = true)
    }
}
