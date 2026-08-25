package cloud.brain.v

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Duration
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * The last thing V said about the day, kept on disk so a widget can draw.
 *
 * A widget is not a small chat window. It gets a few hundred milliseconds of a
 * process Android may kill mid-draw, it cannot show a spinner without lying,
 * and for most of the day the phone is on a network from which the laptop is
 * simply not reachable. So nothing here ever blocks on the network: the file
 * is the source of truth for what gets painted, a fetch replaces the file, and
 * a redraw follows. Offline, the widget shows the last true thing and says
 * when it was true — which is the same rule the cost line follows, applied to
 * a card on a home screen.
 */
object Snapshot {

    private const val FILE = "glance.json"
    private val lock = Mutex()

    /** Times arrive as naive local ISO (`2026-08-25T14:00`) because the server
     *  pins `TZ=Asia/Tehran` and this phone lives in it. Parsing them as local
     *  is therefore right, and is the one assumption in this file: a phone
     *  taken to another zone would show gaps off by the difference. */
    private val ISO: DateTimeFormatter = DateTimeFormatter.ISO_LOCAL_DATE_TIME

    // ---- the shape the widgets draw --------------------------------------
    data class Ev(val what: String, val where: String, val when_: String,
                  val date: java.time.LocalDate?, val at: LocalDateTime?,
                  val allDay: Boolean, val past: Boolean, val today: Boolean)

    data class Item(val id: Long, val text: String, val where: String,
                    val due: String, val days: Int, val overdue: Boolean,
                    val doing: Boolean)

    data class Counts(val open: Int, val overdue: Int, val dueToday: Int,
                      val events: Int, val left: Int, val reminders: Int,
                      val closedLast7: Int)

    data class Glance(
        val headline: String,
        val next: Ev?,
        val events: List<Ev>,
        val tasks: List<Item>,
        val counts: Counts,
        /** When the phone received this, by the phone's clock — not the
         *  server's `at`. It is the honest answer to "how old is this card",
         *  and the two disagree exactly when the network is the problem. */
        val fetched: LocalDateTime,
        /** Ticked from the home screen, not yet confirmed by a fetch. Held so
         *  a box stays ticked across the redraw that happens before the
         *  request comes back, instead of flicking open again. */
        val done: Set<Long>,
    ) {
        fun tasksLeft(): List<Item> = tasks.filterNot { it.id in done }

        /** False on a phone that has never reached V — a legitimate state on
         *  a fresh install, and not the same thing as "the day is empty". */
        val everFetched: Boolean get() = fetched != LocalDateTime.MIN

        /** Older than this and the card says so in amber. Half an hour is the
         *  floor Android puts on a widget's own update period, so anything
         *  under it is simply "the last scheduled redraw". */
        fun stale(now: LocalDateTime = LocalDateTime.now()): Boolean =
            Duration.between(fetched, now).toMinutes() >= 35

        fun age(now: LocalDateTime = LocalDateTime.now()): String =
            fetched.format(DateTimeFormatter.ofPattern("HH:mm"))
    }

    /** What a widget draws before V has ever been reached. Not an error
     *  state — a phone that has just been set up is in it legitimately. */
    val EMPTY = Glance("", null, emptyList(), emptyList(),
                       Counts(0, 0, 0, 0, 0, 0, 0),
                       LocalDateTime.MIN, emptySet())

    // ---- disk -------------------------------------------------------------
    private fun file(context: Context) = File(context.filesDir, FILE)

    suspend fun load(context: Context): Glance = withContext(Dispatchers.IO) {
        lock.withLock { read(context) }
    }

    private fun read(context: Context): Glance {
        val f = file(context)
        if (!f.exists()) return EMPTY
        return try {
            parse(JSONObject(f.readText()))
        } catch (e: Exception) {
            // A half-written or older-format file is not worth crashing a
            // home screen over; it is worth exactly one blank card.
            EMPTY
        }
    }

    /** Replace the card with what the server just said, keeping only the
     *  optimistic ticks that the server has not yet caught up with. */
    suspend fun store(context: Context, server: JSONObject) = withContext(Dispatchers.IO) {
        lock.withLock {
            val previous = read(context)
            val ids = server.optJSONArray("tasks")?.let { a ->
                (0 until a.length()).map { a.getJSONObject(it).optLong("id") }
            }?.toSet() ?: emptySet()
            // A tick is forgotten the moment the task stops coming back open —
            // that is the server confirming it. Keeping it longer would mean a
            // task reopened by hand in Obsidian stayed struck through here.
            val pending = previous.done.filter { it in ids }
            server.put("fetched", LocalDateTime.now().format(ISO))
            server.put("pending", JSONArray(pending))
            write(context, server)
        }
    }

    /** Tick a box locally, right now, before the request goes anywhere. */
    suspend fun markDone(context: Context, id: Long) = withContext(Dispatchers.IO) {
        lock.withLock {
            val f = file(context)
            if (!f.exists()) return@withLock
            try {
                val o = JSONObject(f.readText())
                val pending = o.optJSONArray("pending") ?: JSONArray()
                pending.put(id)
                o.put("pending", pending)
                write(context, o)
            } catch (e: Exception) {
                // Nothing to do: the request still goes out, and the next
                // fetch is what makes the box agree with the note.
            }
        }
    }

    private fun write(context: Context, o: JSONObject) {
        val f = file(context)
        // Written beside and renamed, because the reader is a different
        // process on a different schedule and a truncated file is a blank
        // home screen until the next fetch.
        val tmp = File(f.parentFile, "$FILE.tmp")
        tmp.writeText(o.toString())
        if (!tmp.renameTo(f)) { f.writeText(o.toString()); tmp.delete() }
    }

    // ---- parsing ----------------------------------------------------------
    private fun parse(o: JSONObject): Glance {
        val counts = o.optJSONObject("counts") ?: JSONObject()
        return Glance(
            headline = o.optString("headline"),
            next = o.optJSONObject("next")?.let { ev(it) },
            events = o.optJSONArray("events").map { ev(it) },
            tasks = o.optJSONArray("tasks").map { item(it) },
            counts = Counts(
                open = counts.optInt("open"),
                overdue = counts.optInt("overdue"),
                dueToday = counts.optInt("due_today"),
                events = counts.optInt("events"),
                left = counts.optInt("left"),
                reminders = counts.optInt("reminders"),
                closedLast7 = counts.optInt("closed_last_7")),
            fetched = time(o.optString("fetched")) ?: LocalDateTime.MIN,
            done = o.optJSONArray("pending")?.let { a ->
                (0 until a.length()).map { a.getLong(it) }.toSet()
            } ?: emptySet())
    }

    private fun ev(o: JSONObject) = Ev(
        what = o.optString("what"),
        where = o.optString("where"),
        when_ = o.optString("when"),
        date = try { java.time.LocalDate.parse(o.optString("date")) }
               catch (e: Exception) { null },
        at = if (o.isNull("at")) null else time(o.optString("at")),
        allDay = o.optBoolean("all_day"),
        past = o.optBoolean("past"),
        today = o.optBoolean("today", true))

    private fun item(o: JSONObject) = Item(
        id = o.optLong("id"),
        text = o.optString("text"),
        where = o.optString("where"),
        due = o.optString("due"),
        days = o.optInt("days"),
        overdue = o.optBoolean("overdue"),
        doing = o.optBoolean("doing"))

    private fun time(s: String): LocalDateTime? =
        try { if (s.isBlank()) null else LocalDateTime.parse(s, ISO) }
        catch (e: Exception) { null }

    private fun <T> JSONArray?.map(f: (JSONObject) -> T): List<T> =
        if (this == null) emptyList()
        else (0 until length()).map { f(getJSONObject(it)) }

    // ---- labels -----------------------------------------------------------
    /**
     * `in 42m` · `in 3h 10m` · `now` · `2h ago` · `Wed 09:00`.
     *
     * Computed here rather than sent by the server, and that is the whole
     * reason `/v1/glance` deals in instants. A widget on screen is a picture
     * taken when it was last redrawn — up to half an hour ago — so a label
     * baked server-side would go on saying "in 42m" long after the meeting
     * started. This runs at draw time, so it is right at the moment anyone
     * actually looks at it.
     */
    fun gap(at: LocalDateTime?, now: LocalDateTime = LocalDateTime.now()): String {
        if (at == null) return ""
        val minutes = Duration.between(now, at).toMinutes()
        return when {
            minutes in -1..1 -> "now"
            minutes < 0 -> ago(-minutes)
            minutes < 60 -> "in ${minutes}m"
            minutes < 60 * 10 -> {
                val h = minutes / 60
                val m = minutes % 60
                if (m == 0L) "in ${h}h" else "in ${h}h ${m}m"
            }
            else -> at.format(DateTimeFormatter.ofPattern("EEE HH:mm"))
        }
    }

    /**
     * How far off a day is: nothing for today, `tomorrow`, a weekday, a date.
     *
     * `next_up` looks two weeks ahead, so at 9pm on a Friday the next thing on
     * the calendar is genuinely Monday — and a card that answered "tomorrow"
     * to everything that was not today, which is what the first version of
     * this did, would have been wrong for the entire weekend.
     */
    fun dayLabel(date: java.time.LocalDate?,
                 today: java.time.LocalDate = java.time.LocalDate.now()): String {
        if (date == null) return ""
        val days = java.time.temporal.ChronoUnit.DAYS.between(today, date)
        return when {
            days == 0L -> ""
            days == 1L -> "tomorrow"
            days in 2..6 -> date.format(DateTimeFormatter.ofPattern("EEEE"))
            else -> date.format(DateTimeFormatter.ofPattern("d MMM"))
        }
    }

    private fun ago(minutes: Long): String = when {
        minutes < 60 -> "${minutes}m ago"
        minutes < 60 * 24 -> "${minutes / 60}h ago"
        else -> "${minutes / (60 * 24)}d ago"
    }

    /** The right-hand column of a task row: `5d` for something five days past
     *  its date, `today`, `Tue`, or nothing at all for an undated task. */
    fun dueLabel(item: Item, today: java.time.LocalDate = java.time.LocalDate.now()): String {
        if (item.due.isBlank()) return if (item.days > 0) "${item.days}d" else ""
        val due = try { java.time.LocalDate.parse(item.due) } catch (e: Exception) { return "" }
        val days = java.time.temporal.ChronoUnit.DAYS.between(today, due)
        return when {
            days == 0L -> "today"
            days == 1L -> "tmrw"
            days < 0 -> "${-days}d"
            days <= 6 -> due.format(DateTimeFormatter.ofPattern("EEE"))
            else -> due.format(DateTimeFormatter.ofPattern("d MMM"))
        }
    }
}
