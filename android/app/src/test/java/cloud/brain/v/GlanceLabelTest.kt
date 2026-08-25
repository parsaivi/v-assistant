package cloud.brain.v

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.LocalDate
import java.time.LocalDateTime

/**
 * The labels a widget draws, which are the whole reason `/v1/glance` deals in
 * instants and never in words.
 *
 * A card on a home screen is a picture taken when it was last redrawn — up to
 * half an hour ago, and longer when the laptop is asleep. Every "in 42m" on it
 * is therefore computed from an absolute time at draw time, and this is where
 * that arithmetic is checked. It fails quietly if it is wrong: the card still
 * looks right, it is just describing a meeting that started twenty minutes
 * ago as though it had not.
 */
class GlanceLabelTest {

    private val now: LocalDateTime = LocalDateTime.of(2026, 8, 25, 13, 18)
    private val today: LocalDate = now.toLocalDate()

    private fun at(h: Int, m: Int) = LocalDateTime.of(2026, 8, 25, h, m)

    // ---- how long until ---------------------------------------------------
    @Test
    fun `minutes under an hour`() {
        assertEquals("in 42m", Snapshot.gap(at(14, 0), now))
    }

    @Test
    fun `hours carry their minutes`() {
        assertEquals("in 3h 42m", Snapshot.gap(at(17, 0), now))
        assertEquals("in 2h", Snapshot.gap(at(15, 18), now))
    }

    @Test
    fun `the minute either side of the hour is now`() {
        assertEquals("now", Snapshot.gap(at(13, 18), now))
        assertEquals("now", Snapshot.gap(at(13, 19), now))
    }

    @Test
    fun `something already started says so`() {
        // The failure this exists to catch. A stale card that goes on saying
        // "in 12m" about a meeting that began at noon is worse than a blank
        // one, because it is confidently wrong at exactly the moment it is
        // read.
        assertEquals("18m ago", Snapshot.gap(at(13, 0), now))
        assertEquals("2h ago", Snapshot.gap(at(11, 0), now))
    }

    @Test
    fun `far enough out and it becomes a clock time`() {
        // Ten hours is where "in 11h" stops being useful and the day does the
        // work instead.
        assertEquals("Wed 09:00",
                     Snapshot.gap(LocalDateTime.of(2026, 8, 26, 9, 0), now))
    }

    @Test
    fun `an all-day event has no instant and gets no label`() {
        assertEquals("", Snapshot.gap(null, now))
    }

    // ---- which day --------------------------------------------------------
    @Test
    fun `not today is not the same as tomorrow`() {
        // `next_up` looks two weeks ahead, so at 9pm on a Friday the next
        // thing on the calendar is genuinely Monday. Answering "tomorrow" to
        // everything that was not today — the first version of this — was
        // wrong for the whole weekend.
        assertEquals("", Snapshot.dayLabel(today, today))
        assertEquals("tomorrow", Snapshot.dayLabel(today.plusDays(1), today))
        assertEquals("Friday", Snapshot.dayLabel(today.plusDays(3), today))
        assertEquals("8 Sep", Snapshot.dayLabel(today.plusDays(14), today))
    }

    // ---- when it was due --------------------------------------------------
    private fun task(due: String, days: Int = 0) =
        Snapshot.Item(1L, "t", "Tasks", due, days, days > 0, false)

    @Test
    fun `an overdue task counts the days`() {
        assertEquals("30d", Snapshot.dueLabel(task("2026-07-26"), today))
    }

    @Test
    fun `due today and tomorrow are words`() {
        assertEquals("today", Snapshot.dueLabel(task("2026-08-25"), today))
        assertEquals("tmrw", Snapshot.dueLabel(task("2026-08-26"), today))
    }

    @Test
    fun `an undated task falls back to how long it has been carried`() {
        // Most tasks in this vault have no date marker at all; their age comes
        // from the daily note they were written in, and the server sends it as
        // `days`. Showing nothing there would leave the whole right-hand
        // column of the card empty for the common case.
        assertEquals("11d", Snapshot.dueLabel(task("", days = 11), today))
        assertEquals("", Snapshot.dueLabel(task("", days = 0), today))
    }
}
