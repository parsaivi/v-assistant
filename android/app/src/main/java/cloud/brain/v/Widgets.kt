package cloud.brain.v

import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.updateAll
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import java.time.Duration
import java.time.LocalDateTime

/**
 * The two cards on the home screen, and the one fetch that feeds them both.
 *
 * They are deliberately not two features. `Today` and `Priorities` draw
 * different halves of the same `/v1/glance` snapshot, so putting both on a
 * home screen costs one request rather than two — which matters because the
 * expensive part of reaching V from a widget is not the query (it is free and
 * runs in milliseconds) but waking a radio and completing a mutual-TLS
 * handshake to a laptop that is usually asleep.
 *
 * Nothing here is allowed to be the reason a home screen is blank. Every path
 * draws the cached snapshot first and lets the network arrive late or not at
 * all.
 */
object Widgets {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val gate = Mutex()

    /** The last successful fetch, so three widgets, a service event and a
     *  scheduled update arriving together make one request instead of five. */
    @Volatile private var lastFetch: LocalDateTime = LocalDateTime.MIN

    /** Below this, a refresh is served from what is already on disk. Long
     *  enough to collapse a burst, short enough that pressing the header still
     *  feels like it did something. */
    private const val COALESCE_SECONDS = 20L

    /** Fire and forget: refresh if it is worth it, then redraw either way. */
    fun refresh(context: Context, force: Boolean = false) {
        val app = context.applicationContext
        scope.launch { refreshNow(app, force) }
    }

    /**
     * Fetch, store, redraw. Returns having redrawn whatever it ended up with.
     *
     * The timeout is the point of the whole function. A widget update is given
     * about ten seconds by Android before the process is a candidate for
     * death, and `Api` will happily wait five minutes for a model to think —
     * right for a chat turn, fatal here. `/v1/glance` never reaches a model,
     * so anything slower than a few seconds is a network that is not going to
     * answer, and the card is better off staying honest about its age.
     */
    suspend fun refreshNow(context: Context, force: Boolean = false) {
        gate.withLock {
            val since = Duration.between(lastFetch, LocalDateTime.now()).seconds
            if (force || since >= COALESCE_SECONDS) {
                val body = withTimeoutOrNull(8_000) {
                    try {
                        Api(context).glance()
                    } catch (e: Exception) {
                        // Unreachable, revoked, asleep. All the same answer
                        // here: keep the card, keep its timestamp, let it go
                        // amber. A widget has nowhere to put an error.
                        null
                    }
                }
                if (body != null) {
                    Snapshot.store(context, body)
                    lastFetch = LocalDateTime.now()
                }
            }
        }
        // Outside the lock, and unconditional: a coalesced call still has a
        // card to draw, and the clock has moved even when the data has not —
        // which is exactly what "in 42m" becoming "in 12m" depends on.
        redraw(context)
    }

    /** Redraw both cards from disk without going near the network. */
    suspend fun redraw(context: Context) {
        try {
            TodayWidget().updateAll(context)
            PrioritiesWidget().updateAll(context)
        } catch (e: Exception) {
            // No widget of that type on the home screen. Not a failure.
        }
    }

    /** Called from `VService` when the event stream delivers anything at all.
     *
     *  This is the part a cloud-backed widget cannot do. The service already
     *  holds an authenticated socket to the machine that owns the data, so a
     *  reminder firing or the 10:00 brief landing redraws the home screen at
     *  the moment it happens — no polling interval, no push service, and no
     *  third party in the path.
     */
    fun onEvent(context: Context) = refresh(context, force = true)
}

/**
 * Both receivers do the same two things, so they say so once.
 *
 * `onUpdate` is where Android's own half-hourly tick arrives, and it is the
 * only refresh that still works when `VService` has been killed — so it is the
 * floor under the whole feature rather than a nicety. `goAsync` is what buys
 * the request the few seconds it needs; without it the broadcast returns and
 * the process may be gone before the socket is open.
 */
abstract class VWidgetReceiver : GlanceAppWidgetReceiver() {

    override fun onUpdate(context: Context, manager: AppWidgetManager,
                          ids: IntArray) {
        super.onUpdate(context, manager, ids)
        val pending = goAsync()
        val app = context.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                Widgets.refreshNow(app)
            } finally {
                pending.finish()
            }
        }
    }
}

class TodayWidgetReceiver : VWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = TodayWidget()
}

class PrioritiesWidgetReceiver : VWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = PrioritiesWidget()
}

/** Opening the app from a card. Plain launch — a widget tap is "show me the
 *  rest of this", not a new conversation. */
internal fun openApp(context: Context): Intent =
    Intent(context, MainActivity::class.java)
        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
