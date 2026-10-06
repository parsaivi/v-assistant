"""The proactive loop — the layer that lets the assistant speak first.

No APScheduler dependency: the job set is small and fixed, and rolling it by
hand buys the one behaviour that actually matters — **catch-up on boot**. A
two-hour outage must not silently eat reminders or skip the morning brief.

Nothing here calls a model. Jobs assemble their content from vault queries and
hand back text; the transport (CLI now, Telegram later) decides how to deliver.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from . import briefing
from .config import Settings, settings as default_settings


# The scheduler's own record of when each job last fired, readable without
# building one. `Scheduler.__init__` takes an `Engine`, which the MCP server
# has no business constructing on a tool call — but "when did the nightly
# Quera sync last run?" is exactly the question an agent needs answered before
# it tells Parsa anything about whether his deadlines are current. Reading the
# state file it already writes is cheaper and truer than a second store.
QUERA_JOB = "quera-sync"


def last_run(name: str, settings: Settings | None = None) -> dt.datetime | None:
    """When job `name` last completed, or None if it never has."""
    s = settings or default_settings
    try:
        raw = json.loads((s.state_dir / "scheduler.json").read_text(
            encoding="utf-8")).get(name)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    try:
        return dt.datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


@dataclass
class Job:
    name: str
    at: dt.time | None          # daily wall-clock time, or None for interval jobs
    fn: Callable[[], object]
    weekdays: tuple[int, ...] | None = None   # 0=Mon .. 6=Sun
    interval_minutes: int | None = None
    catch_up: bool = True       # run late if we were down when it was due
    # Does this job's return value belong to *Parsa*, or to whoever is
    # debugging V? A reminder, a new email, the evening close-out are
    # messages. "calendar: +0 imported, 13 already there" is a log line that
    # happened to be returned from a function — and pushing it to Telegram is
    # how a channel that should be worth reading becomes one you swipe away
    # without looking, taking the reminders with it. Housekeeping jobs set
    # this False and their output goes to `logs/scheduler.log` instead.
    announce: bool = True


class Scheduler:
    def __init__(self, engine, settings: Settings | None = None, sink=None):
        self.engine = engine
        self.s = settings or engine.s
        self.sink = sink or self._default_sink
        self.state_path = self.s.state_dir / "scheduler.json"
        self.state = self._load_state()
        self.jobs = self._build_jobs()
        # Looked up by name in `_deliver`, because by then the job object is
        # gone — `tick` and `catch_up` both hand over a `(name, result)` pair.
        self.silent = {j.name for j in self.jobs if not j.announce}
        # Whether anyone is watching stdout. `v scheduler` in a terminal is a
        # deliberate, foreground run and should show its work; `v.service`
        # feeding Telegram is not, and its housekeeping goes to the log alone.
        self.foreground = sink is None

    # ---- jobs --------------------------------------------------------
    def _build_jobs(self) -> list[Job]:
        return [
            # 10:00, not 07:00, and it *delivers* rather than returning text
            # into whatever stdout happened to be attached. The standing
            # briefing is installed as a crontab entry (`cli briefing
            # --install-cron`), so it fires whether or not this process is
            # running; both paths go through `briefing.run`, which records the
            # day it delivered, so the two cannot push the same morning twice.
            # `announce=False` and it is not an oversight: `briefing.run`
            # *delivers the brief itself*, over notify-send, the daily note and
            # Telegram. What this job hands back is a one-line receipt saying
            # whether that happened — "morning brief not pushed — already
            # delivered at 12:14" is a sentence about V's plumbing, arriving
            # right underneath the brief it is talking about.
            Job("morning-brief", briefing.DEFAULT_TIME, self._morning_brief,
                announce=False),
            Job("evening-closeout", dt.time(21, 0), self.engine.evening),
            # Friday is the last day of this vault's Sat→Fri week.
            Job("weekly-rollup", dt.time(18, 0), self._weekly, weekdays=(4,)),
            Job("reminders", None, self._fire_reminders, interval_minutes=1,
                catch_up=False),
            # Hourly rather than daily, because "as they land" is the point:
            # an MR merged at 11 should be in the brag document that afternoon,
            # while you still remember what it did. Costs one SQL query and no
            # model, and writes nothing on an hour where nothing closed.
            Job("brag-drafts", None, self._brag, interval_minutes=60,
                catch_up=False),
            Job("reindex", None, self._reindex, interval_minutes=15,
                catch_up=False, announce=False),
            # Embeddings are a separate artefact from the note mirror and cost
            # minutes, not milliseconds, so they can't ride on the 15-minute
            # reindex. Nightly is often enough: the gap only affects semantic
            # search, and `status` says out loud when it has fallen behind.
            Job("re-embed", dt.time(4, 30), self._reembed, announce=False),
            # V's own notes, kept current. A skills page that describes last
            # week's build is worse than none — it is the page you'd check to
            # find out why V is behaving the way it is.
            Job("self-docs", dt.time(5, 0), self._self_docs, announce=False),
            # Before the brief, so "Today" is right when it runs at 07:00.
            # Pulls the remote feed into the vault calendar and renders the
            # coming fortnight into the daily notes, keeping one source of
            # truth instead of a feed, a section, and a reminders table
            # disagreeing with each other.
            Job("calendar-sync", dt.time(6, 30), self._calendar_sync,
                announce=False),
            # Saturday opens this vault's week, so the week note has to exist
            # and be seeded before the first daily note of it embeds anything.
            # Early, and before `calendar-sync` would render the day: a daily
            # note whose `## ⏱ Schedule` says "not synced yet" all Saturday is
            # the one failure that would make the whole feature feel dead.
            Job("week-scaffold", dt.time(5, 30), self._week_scaffold,
                weekdays=(5,), announce=False),
            # Every 10 minutes, not every minute like reminders: an unread
            # email is not as time-critical as a reminder Parsa set for a
            # specific minute, and Gmail's list endpoint is not free to poll
            # constantly. `Gmail.unseen` owns its own dedup state, so a
            # message already pushed here never surfaces twice.
            Job("gmail-check", None, self._gmail_check, interval_minutes=10,
                catch_up=False),
            # Once a day is enough for deadlines — they don't change by the
            # minute the way email does, and this is what feeds
            # `nudges.assignments_due`, which the morning brief already
            # reads. Before the brief, same reasoning as calendar-sync.
            Job("quera-sync", dt.time(6, 45), self._quera_sync),
            # The day's one Navasan request, so the RATES card and its history
            # have a point for every day even when nobody opened the HUD. A
            # no-op when someone already looked today — the first reader of a
            # day is the one who fetches, whichever process that is. 10:00 so
            # the morning market has moved; the free plan lags two hours.
            Job("rates", dt.time(10, 0), self._rates, announce=False),
            # Every five minutes, and cheap when there is nothing to do: it
            # reads one small JSON file and usually finds it empty. The
            # interval is not the retry rate — each parked question carries
            # its own reset time — it is only how quickly V notices that one
            # has come due.
            Job("deferred-answers", None, self._deferred, interval_minutes=5,
                catch_up=False),
            # Files V made while nobody was asking. `vault_send_file` runs
            # inside whatever process an agent turn spawned, which is not the
            # process holding a socket to the phone, so it leaves a row in the
            # outbox instead — and this is the sweep that gets a chart rendered
            # at 3am onto the phone in the morning. A chat turn drains the same
            # queue first when there is one, so this usually finds it empty;
            # `Reminders.claim` is what makes two drainers safe.
            Job("outbox", None, self._outbox, interval_minutes=1,
                catch_up=False),
        ]

    # ---- the loop ----------------------------------------------------
    def run(self, *, once: bool = False, interval: int = 30) -> None:
        missed = self.catch_up()
        for item in missed:
            self._deliver("catch-up", item)

        if once:
            for item in self.tick():
                self._deliver("tick", item)
            return

        print(f"scheduler running · {len(self.jobs)} jobs · polling every {interval}s")
        try:
            while True:
                for item in self.tick():
                    self._deliver("tick", item)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped")

    # ---- delivery ----------------------------------------------------
    def _deliver(self, source: str, item) -> None:
        """Hand one job's output to the sink, and settle what arrived.

        The sink contract: return `False` to say *nothing in this item reached
        anyone*. `None` (what a sink that merely prints returns) and `True`
        both mean delivered, so a surface with nothing to report doesn't have
        to care.

        Only reminders are settled, because only reminders have a row that a
        premature write can destroy. A morning brief that fails to send is
        regrettable; a reminder that fails to send used to be *gone*, marked
        delivered in SQLite and invisible in `pending()`. Now an unconfirmed
        reminder stays due and the one-minute tick tries it again.

        The cost of that is a possible duplicate: a sink that delivered two
        reminders and failed on a third reports `False` for the item, and all
        three are retried. Seeing a reminder twice is a smaller failure than
        never seeing it at all, and this is the trade the whole change is
        about.
        """
        name = (item[0] if isinstance(item, tuple) else source)
        # `catch_up` labels a late job "<name> (missed)"; the job is the same
        # job, and a receipt is no more worth pushing for having been late.
        if str(name).removesuffix(" (missed)") in self.silent:
            self._log(name, item)
            return

        if self.sink(source, item) is False:
            return          # left due on purpose — the next tick retries it
        _, payload = item if isinstance(item, tuple) else (source, item)
        for p in (payload if isinstance(payload, list) else [payload]):
            if getattr(p, "route", "") != "reminder:fire":
                continue
            rid = (getattr(p, "data", None) or {}).get("reminder_id")
            if rid is not None:
                try:
                    self.engine.settle_reminder(rid)
                except Exception:       # noqa: BLE001 — a retry beats a crash
                    pass

    def _log(self, name, item) -> None:
        """Where a housekeeping job's output actually goes.

        A file, not a chat and not a swallow: "did the nightly re-embed
        finish?" has to stay answerable, and `logs/scheduler.log` is where you
        look — the same place the crontab briefing already writes. Best-effort
        by construction, because a scheduler that dies on a full disk while
        trying to record that it did some tidying is worse than one that tidies
        quietly.
        """
        _, payload = item if isinstance(item, tuple) else (name, item)
        items = payload if isinstance(payload, list) else [payload]
        lines = []
        for p in items:
            if p is None:
                continue
            text = getattr(p, "render", lambda: str(p))().strip()
            if text:
                lines.append(" ".join(text.split()))
        if not lines:
            return          # the job had nothing to say; nothing to record
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        entry = "".join(f"{stamp} {name} · {line}\n" for line in lines)
        if self.foreground:
            print(entry, end="")
        try:
            self.s.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.s.log_dir / "scheduler.log").open(
                    "a", encoding="utf-8") as fh:
                fh.write(entry)
        except OSError:
            pass

    def tick(self, now: dt.datetime | None = None) -> list[tuple[str, object]]:
        now = now or dt.datetime.now()
        out = []
        for job in self.jobs:
            if self._should_run(job, now):
                out.append((job.name, self._run(job, now)))
        return out

    def catch_up(self, now: dt.datetime | None = None) -> list[tuple[str, object]]:
        """Run anything whose slot passed while the process was not running.

        This is the whole reason the scheduler is hand-rolled. A reminder that
        silently never fires is the worst failure this system can have.
        """
        now = now or dt.datetime.now()
        out = []

        due = self.engine.reminders.catch_up(now=now, grace_minutes=1)
        if due:
            out.append(("reminders-missed", self.engine.due_reminders(now)))

        for job in self.jobs:
            if not job.catch_up or job.at is None:
                continue
            last = self._last_run(job.name)
            slot = dt.datetime.combine(now.date(), job.at)
            if slot > now:
                slot -= dt.timedelta(days=1)
            if job.weekdays and slot.weekday() not in job.weekdays:
                continue
            if last is None or last < slot:
                out.append((f"{job.name} (missed)", self._run(job, now)))
        return out

    # ---- internals ---------------------------------------------------
    def _should_run(self, job: Job, now: dt.datetime) -> bool:
        last = self._last_run(job.name)

        if job.interval_minutes is not None:
            return last is None or (now - last).total_seconds() >= job.interval_minutes * 60

        if job.weekdays and now.weekday() not in job.weekdays:
            return False
        slot = dt.datetime.combine(now.date(), job.at)
        if now < slot:
            return False
        if self._quiet(now) and job.name != "reminders":
            return False
        return last is None or last < slot

    def _quiet(self, now: dt.datetime) -> bool:
        start, end = self.s.quiet_hours
        if start <= now.hour < end:
            return True
        # `/quiet` in the REPL, read from disk on every check — see
        # `briefing.muted`, which the cron path shares so that one switch
        # silences V no matter which process is doing the talking.
        return briefing.muted(self.s, now)

    def _run(self, job: Job, now: dt.datetime):
        try:
            result = job.fn()
        except Exception as exc:
            result = f"job {job.name} failed: {exc}"
        self.state[job.name] = now.isoformat(timespec="seconds")
        self._save_state()
        return result

    def _last_run(self, name: str) -> dt.datetime | None:
        raw = self.state.get(name)
        return dt.datetime.fromisoformat(raw) if raw else None

    def _morning_brief(self):
        brief = briefing.run(self.engine)
        if brief.delivered:
            return f"morning brief pushed via {', '.join(brief.channels)}"
        return f"morning brief not pushed — {brief.reason}"

    def _brag(self):
        report = self.engine.brag.run()
        if report.get("seeded"):
            return (f"brag: seeded {report['seeded']} already-closed items "
                    f"(nothing drafted — `cli brag --backfill` to catch up)")
        if not report["drafted"]:
            return None      # silence is correct: nothing closed this hour
        return ("brag: drafted "
                + ", ".join(d["wrote"] for d in report["drafted"]))

    def _fire_reminders(self):
        return self.engine.due_reminders()

    def _outbox(self):
        """Hand over anything queued, as ordinary responses.

        Returned as `Response`s rather than published directly because this
        object does not know what a bus is — the sink does, and the same job
        has to work when the sink is Telegram, a terminal, or nothing at all.
        Which also means the Telegram surface gets these for free the day it
        wants them, without a second queue.
        """
        from .engine import Response
        from .files import take_queued_files
        out = []
        for meta in take_queued_files(self.engine):
            caption = meta.get("caption") or ""
            text = f"{caption}\n\n" if caption else ""
            out.append(Response(f"{text}`{meta['rel']}`",
                                data={"files": [meta["rel"]]},
                                route="outbox:file"))
        return out or None

    def _reindex(self):
        from . import index
        stats = index.rebuild(self.s)
        if not stats.updated and not stats.removed:
            return None      # silence is correct: nothing changed this pass
        # `IndexStats.__str__` is already the line you'd want in a log, and
        # this is a log now — no reason to keep a second, thinner copy of it.
        return f"reindex: {stats}"

    def _reembed(self):
        """Rebuild the semantic index so notes written today are searchable."""
        st = self.engine.rag.build() or {}
        if not isinstance(st, dict):
            return st
        return (f"re-embed: {st.get('chunks', 0)} chunks · "
                f"{st.get('backend', '?')} · {st.get('seconds', 0)}s")

    def _self_docs(self):
        from . import identity
        self.engine.journal.refresh_all(
            capabilities=identity.capabilities(self.engine),
            goals=identity.GOALS,
            memory_markdown=self.engine.memory.export_markdown(),
        )
        return "refreshed 19 - Assistant/V/"

    def _week_scaffold(self):
        """Create and seed this week's note, then render it into the dailies.

        The V half of "both". Templater does this when Parsa makes the note in
        Obsidian; this is for the weeks he doesn't, and `reseed` is what makes
        running after Templater harmless — it is append-only and deduped, so a
        note Obsidian already filled gains nothing.
        """
        wp = self.engine.q.weekplan
        day = dt.date.today()
        rel = wp.ensure_week(day)
        added = wp.reseed(day)
        synced = sum(1 for i in range(7)
                     if wp.sync_daily_note(day + dt.timedelta(days=i)))
        return (f"week note {rel}"
                + (f" · +{added} repeating block(s)" if added else "")
                + (f" · {synced} daily note(s) synced" if synced else ""))

    def _calendar_sync(self):
        cal = self.engine.q.calendar
        today = dt.date.today()
        # `import_ical` isolates each day itself, so it returns real counts
        # even when part of the feed is unreachable — no blanket try/except
        # here, which is what previously turned a partial import into a
        # reported zero.
        added, skipped = cal.import_ical(today, days=14)
        synced = sum(1 for i in range(14)
                     if cal.sync_daily_note(today + dt.timedelta(days=i)))
        return (f"calendar: +{added} imported, {skipped} already there, "
                f"{synced} daily note(s) rendered")

    def _gmail_check(self):
        """New unread mail, pushed the same way a reminder is. Silent
        (`None`) when Gmail isn't connected at all or a call fails — a
        transient Google hiccup on a job that retries again in ten minutes
        is not worth an unprompted message, and `cli status` already says
        whether Gmail is connected for anyone checking on purpose."""
        from . import gmail as gmail_mod

        g = gmail_mod.Gmail(self.s)
        if not (g.configured() and g.authorised()):
            return None
        try:
            new = g.unseen("is:unread newer_than:1d", limit=10)
        except gmail_mod.GmailError:
            return None
        if not new:
            return None
        lines = [f"{len(new)} new email(s):"]
        lines += [f"- {h.render()}" for h in new]
        return "\n".join(lines)

    def _quera_sync(self):
        """Pull deadlines into `assignment` notes. `nudges.assignments_due`
        is what actually reminds Parsa — this job's own return value is just
        the day's sync log, or a surfaced error when the session has expired
        (unlike a missing Gmail connection, worth repeating daily until
        fixed: a silently-stopped deadline sync is exactly the kind of
        confidently-wrong-by-omission failure this vault tries hardest to
        avoid)."""
        from . import quera as quera_mod

        q = quera_mod.Quera(self.s, self.engine.vault)
        if not q.configured():
            return None
        report = q.sync(dry_run=False)
        if report.get("error"):
            return f"quera: {report['error']}"
        created, updated = report["created"], report["updated"]
        if not created and not updated:
            return None
        return (f"quera: {len(created)} new assignment(s), "
                f"{len(updated)} deadline(s) updated")

    def _rates(self):
        from . import rates as rates_mod

        r = rates_mod.Rates(self.s)
        if not r.configured():
            return None
        board = r.ensure_today()
        if board is None or board.fetched.date() != dt.date.today():
            return f"rates: no board today — {r.failure().get('why', 'not fetched')}"
        usd = board.get("usd_sell")
        return (f"rates: {board.kind} board at {board.fetched:%H:%M}"
                + (f" · dollar {rates_mod.format_value(usd)}" if usd else ""))

    def _deferred(self, now: dt.datetime | None = None):
        """Answer what the plan window turned away earlier.

        Held through quiet hours rather than dropped. The five-hour window
        very often resets at 3am, which is exactly when an unprompted message
        is worth least and costs most — so the question waits for a civil
        hour, and the queue is what makes waiting free.

        This is the one interval job that can spend allowance, and it can only
        spend it on something Parsa already asked for.
        """
        now = now or dt.datetime.now()
        if not self.engine.deferred.due(now):
            return None
        if self._quiet(now):
            return None
        answers = self.engine.answer_deferred(now)
        if not answers:
            return None
        return "\n\n".join(r.render() for r in answers)

    def _weekly(self):
        """Draft the weekly note from the week's dailies. Review, never auto-write."""
        today = dt.date.today()
        # This vault's week runs Saturday → Friday.
        start = today - dt.timedelta(days=(today.weekday() + 2) % 7)
        dailies = self.engine.q.daily_notes(since=start, until=today)
        iso_year, iso_week, _ = today.isocalendar()
        lines = [f"Weekly rollup draft for {iso_year}-W{iso_week:02d} "
                 f"({start} → {today})", ""]
        lines += [f"- [[{r.rel[:-3]}]]" for r in dailies.rows]
        workouts = self.engine.q.workouts(since=start)
        lines.append(f"\nWorkouts: {len(workouts.rows)}")
        return "\n".join(lines)

    # ---- persistence -------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    @staticmethod
    def _default_sink(source: str, item) -> None:
        from . import ui

        name, payload = item if isinstance(item, tuple) else (source, item)
        items = payload if isinstance(payload, list) else [payload]
        items = [p for p in items if p is not None]
        if not items:
            return
        print()
        for p in items:
            # Marked as unprompted, because it is. An interruption that looks
            # exactly like an answer is how you end up scrolling back for a
            # question you never asked — and then muting the thing entirely.
            print(ui.proactive(getattr(p, "render", lambda: str(p))(),
                               footer=f"{name} · unprompted · /quiet to stop"))
