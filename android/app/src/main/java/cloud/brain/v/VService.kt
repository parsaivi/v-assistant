package cloud.brain.v

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.Request
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicLong

/**
 * The half of the app that is running when the app is not.
 *
 * Two jobs, and they are the same job seen from either end:
 *
 *  1. **Proactive.** Holds the `/v1/events` stream open so a reminder, the
 *     10:00 briefing or a nudge becomes a notification. There is no FCM here —
 *     routing a reminder about a dentist appointment through Google's servers,
 *     to arrive on a phone already holding an authenticated socket to the
 *     machine that produced it, would put a third party back in the one path
 *     this transport exists to keep private.
 *
 *  2. **The gate.** Holds the microphone open and runs the wake pipeline on it,
 *     so saying "Friday" starts a conversation with the phone in a pocket and
 *     the app closed. It used to be a headset connecting that started a turn;
 *     that trigger is gone, because the gate listens on the phone's own
 *     microphone and a headset therefore has nothing to do with whether V can
 *     hear. See `Gate` for why that mic and not the headset's.
 *
 * A foreground service, declared `dataSync`, because both are exactly what
 * Android's background limits are designed to stop and neither works if it is
 * stopped. The notification it is legally required to post is used to say
 * something true instead of something apologetic — it is the only place the
 * connection state is visible when the app is closed.
 */
class VService : LifecycleService() {

    companion object {
        const val CHANNEL_STATUS = "v-status"
        const val CHANNEL_PROACTIVE = "v-proactive"
        private const val ONGOING_ID = 1

        /** Set when the start came from the UI, which is the only origin
         *  allowed to arm a microphone service. See [armed]. */
        const val EXTRA_ARM = "arm"

        fun start(context: Context, arm: Boolean = true) {
            val intent = Intent(context, VService::class.java)
                .putExtra(EXTRA_ARM, arm)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Exception) {
                // Android 12+ refuses some background starts outright. Nothing
                // useful to do here: the next time the app is opened it starts
                // from the foreground and succeeds.
            }
        }
    }

    private lateinit var api: Api
    private lateinit var player: Player
    private lateinit var prints: Voiceprint
    private var gate: Gate? = null
    private var gateJob: Job? = null
    private var conversation: Job? = null
    private val recorder = Recorder()
    private val notifications by lazy {
        getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    }

    /** The last event delivered, so a reconnect asks for what it missed
     *  rather than replaying the day or losing it. */
    private val seq = AtomicLong(0)
    private var stream: EventSource? = null
    private var streamJob: Job? = null
    private var connected = false

    /**
     * Whether this service may hold the microphone.
     *
     * This exists for one specific platform rule. From Android 14 the
     * `microphone` foreground-service type is a *while-in-use* type, and
     * starting one from the background throws — and `BOOT_COMPLETED`, which is
     * an exemption for starting a foreground service in general, is **not** an
     * exemption for that type. So a reboot brings back the half of this service
     * that can legally run unattended (the event stream, which is what a missed
     * reminder depends on) and leaves the gate disarmed until the app is next
     * opened. One tap after a reboot, and the alternative is a service that
     * crashes on boot forever.
     */
    @Volatile private var armed = false

    // ---- lifecycle --------------------------------------------------------
    override fun onCreate() {
        super.onCreate()
        api = Api(applicationContext)
        player = Player(applicationContext)
        prints = Voiceprint(applicationContext)
        channels()
        foreground("connecting…")
        connect()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        // Re-checked on every start rather than only in onCreate: enrollment
        // finishes while this service is already running, and the gate has to
        // pick that up without the user restarting anything.
        if (intent?.getBooleanExtra(EXTRA_ARM, false) == true) armed = true
        foreground(status())
        if (armed) listen()
        // START_STICKY: if Android kills this for memory, it should come back.
        // A reminder channel that stays down until the app is next opened by
        // hand is a reminder channel that has already failed.
        return START_STICKY
    }

    override fun onDestroy() {
        stream?.cancel()
        streamJob?.cancel()
        gateJob?.cancel()
        conversation?.cancel()
        player.stop()
        player.releaseFocus()
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    // ---- the event stream -------------------------------------------------
    private fun connect() {
        streamJob?.cancel()
        streamJob = lifecycleScope.launch {
            var backoff = 2_000L
            while (true) {
                try {
                    val url = api.eventsUrl(seq.get())
                    open(url)
                    // `open` returns when the stream closes for any reason.
                    backoff = 2_000L
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    connected = false
                    notifications.notify(ONGOING_ID, ongoing("offline"))
                } finally {
                    // Re-probe the address on every reconnect, not only after
                    // an exception. `Api` caches whichever address answered
                    // first and keeps using it, which is right for a burst of
                    // requests and wrong for the thing that most often ends
                    // this stream: the network changing underneath it. Toggling
                    // a VPN moves V from the tailnet address to the LAN one or
                    // back, and the old address does not fail loudly — the
                    // stream just closes. Without this the loop would redial a
                    // dead address forever, having never thrown anything.
                    // One extra `/v1/hello` per reconnect is nothing.
                    api.forgetHost()
                }
                delay(backoff)
                // Backing off to a minute rather than retrying every two
                // seconds forever: the usual reason this fails is the laptop
                // being asleep, which is a state that lasts hours, and a radio
                // woken every two seconds for eight hours is a flat battery.
                backoff = (backoff * 2).coerceAtMost(60_000L)
            }
        }
    }

    private suspend fun open(url: String) {
        val done = kotlinx.coroutines.CompletableDeferred<Unit>()
        val request = Request.Builder().url(url)
            .header("Accept", "text/event-stream").build()
        stream = EventSources.createFactory(Net.client(applicationContext))
            .newEventSource(request, object : EventSourceListener() {
                override fun onOpen(eventSource: EventSource, response: okhttp3.Response) {
                    connected = true
                    notifications.notify(ONGOING_ID, ongoing(status()))
                }

                override fun onEvent(eventSource: EventSource, id: String?,
                                     type: String?, data: String) {
                    id?.toLongOrNull()?.let { seq.set(it) }
                    proactive(type ?: "v", data)
                }

                override fun onClosed(eventSource: EventSource) {
                    connected = false
                    if (!done.isCompleted) done.complete(Unit)
                }

                override fun onFailure(eventSource: EventSource, t: Throwable?,
                                       response: okhttp3.Response?) {
                    connected = false
                    if (!done.isCompleted) done.complete(Unit)
                }
            })
        done.await()
        stream?.cancel()
        stream = null
    }

    /**
     * One event, one notification.
     *
     * "Ignored once → retried tomorrow; ignored twice → channel spent. Never a
     * badge, never a sound." The retry rule lives on the server, where the
     * proactive budget is; what this side owes the design is the *never* —
     * hence no sound, no vibration, and no badge.
     */
    private fun proactive(kind: String, data: String) {
        val text = try {
            JSONObject(data).optString("text").ifBlank { return }
        } catch (e: Exception) {
            return
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val notification = NotificationCompat.Builder(this, CHANNEL_PROACTIVE)
            .setSmallIcon(R.drawable.ic_stat_v)
            .setContentTitle("V")
            .setContentText(text.lineSequence().first())
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setColor(0xFF7C5CFF.toInt())
            .setContentIntent(open)
            .setAutoCancel(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .build()
        notifications.notify(kind.hashCode() and 0xFFFF, notification)
    }

    // ---- the gate --------------------------------------------------------
    /**
     * Start listening, if there is a voiceprint to listen for.
     *
     * Without one the gate has no templates and no centroid, so it would score
     * every utterance against nothing and match none of them. Silently holding
     * the microphone open in that state would be the worst of both worlds —
     * the battery cost and the green dot, with no possibility of it ever
     * firing — so the service simply does not start it and the notification
     * says why.
     */
    private fun listen() {
        if (!armed) return
        if (!prints.isEnrolled) {
            foreground("not enrolled · open V to teach it your voice")
            return
        }
        if (gateJob?.isActive == true) return
        val g = Gate(applicationContext, prints).also { gate = it }
        gateJob = lifecycleScope.launch {
            g.run { pcm -> onWake() }
        }
    }

    /**
     * Both gates passed. Take the microphone off the gate, put the session on
     * screen, and hand over.
     *
     * The gate must yield before the conversation opens its own `AudioRecord`:
     * two recorders on one microphone is not an error Android reports, it is
     * one of them silently getting nothing.
     */
    private fun onWake() {
        if (conversation?.isActive == true) return
        val g = gate ?: return
        g.yieldMic()
        show()
        conversation = lifecycleScope.launch {
            // Wait for the gate to actually put the microphone down before
            // opening another one on it. `yieldMic` only raises a flag; the
            // reader is blocked inside `AudioRecord.read` and notices it up to
            // one buffer later. Two AudioRecords on one microphone is not an
            // error Android reports — the second simply receives silence, and
            // the first turn of every conversation would be empty.
            withTimeoutOrNull(1_500) {
                g.state.first { it != Gate.State.LISTENING }
            }
            try {
                VoiceSession.run(applicationContext, api, player, recorder)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // A conversation that fails in a dead spot is not worth a
                // notification; the next wake tries again.
            } finally {
                g.resumeMic()
                notifications.notify(ONGOING_ID, ongoing(status()))
            }
        }
    }

    /**
     * Bring the conversation to the screen, over the lock screen if need be.
     *
     * Starting an activity from a service is restricted from Android 10, and
     * the exemption this relies on is `SYSTEM_ALERT_WINDOW` — granted once, by
     * hand, in Settings. It is asked for exactly here and nowhere else. If it
     * was never granted the conversation still runs; it just runs without a
     * window, which is why the caption and the state also exist in the
     * notification.
     */
    private fun show() {
        try {
            startActivity(Intent(this, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
                         Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                putExtra(MainActivity.EXTRA_VOICE, true)
            })
        } catch (e: Exception) {
            notifications.notify(ONGOING_ID, ongoing("listening · tap to open"))
        }
    }

    /** What the ongoing notification says — the only place the gate's state is
     *  visible when the app is closed. */
    private fun status(): String = when {
        !prints.isEnrolled -> "not enrolled"
        !armed -> "open V to start listening"
        prints.shadowMode -> "listening · shadow mode · nothing acts yet"
        gateJob?.isActive == true -> "listening for “Friday” · nothing leaves this room"
        else -> "connected"
    }

    // ---- notifications ----------------------------------------------------
    private fun channels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_STATUS, "Connection",
                                NotificationManager.IMPORTANCE_MIN).apply {
                description = "Whether V can be reached. Silent."
                setShowBadge(false)
            })
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_PROACTIVE, "V speaks first",
                                NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Reminders, the morning brief, and nudges."
                // Never a badge, never a sound — the brief says both.
                setShowBadge(false)
                setSound(null, null)
                enableVibration(false)
            })
    }

    /**
     * `startForeground` with the microphone type.
     *
     * From Android 14 a service that records audio must say so here, and the
     * declared type has to match a permission the app actually holds. Before
     * enrollment — and if RECORD_AUDIO was refused — there is no microphone in
     * use, so it starts as a plain data-sync service instead. Declaring
     * `microphone` in that state is not merely dishonest, it throws.
     */
    private fun foreground(state: String) {
        val note = ongoing(state)
        val micAllowed = androidx.core.content.ContextCompat.checkSelfPermission(
            this, android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        val wantsMic = armed && micAllowed && prints.isEnrolled
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val type = if (wantsMic)
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                else ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                startForeground(ONGOING_ID, note, type)
            } else {
                startForeground(ONGOING_ID, note)
            }
        } catch (e: Exception) {
            // The microphone type was refused — a background start on 14+, or
            // a permission revoked while running. Fall back to the data-sync
            // half rather than letting the whole service die and take the
            // reminder channel with it.
            armed = false
            gateJob?.cancel()
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    startForeground(ONGOING_ID, ongoing("open V to start listening"),
                                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
                } else {
                    startForeground(ONGOING_ID, ongoing("open V to start listening"))
                }
            } catch (fatal: Exception) { stopSelf() }
        }
    }

    private fun ongoing(state: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return NotificationCompat.Builder(this, CHANNEL_STATUS)
            .setSmallIcon(R.drawable.ic_stat_v)
            .setContentTitle("V")
            .setContentText(state)
            .setColor(0xFF7C5CFF.toInt())
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
            .setContentIntent(open)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }
}

/**
 * Bring the service back after a reboot, since that is exactly when a phone is
 * most likely to be away from the app for hours.
 *
 * Started **disarmed**: the event stream comes back so a reminder still
 * arrives, and the microphone does not, because Android 14 does not permit a
 * while-in-use foreground service to be started from a boot broadcast. The gate
 * arms itself the next time `MainActivity` runs.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            VService.start(context, arm = false)
        }
    }
}
