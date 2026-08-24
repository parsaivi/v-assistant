"""Nudges: things the vault knows that you don't. Every one costs $0.

The whole point of this module is that none of it is an LLM problem. Each nudge
is a frontmatter query with a threshold. The design doc calls this the highest
value-per-dollar feature in the system, and the arithmetic agrees: the dollar is
zero.

Each nudge carries a `kind` from the ambient-agent vocabulary — notify, question
or review — which forces the question "am I allowed to act here?" at design time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable

from . import notes, query, schema
from .config import Settings, settings as default_settings

NOTIFY, QUESTION, REVIEW = "notify", "question", "review"


@dataclass
class Nudge:
    id: str
    kind: str
    urgency: int          # 0 = FYI, 3 = act today
    text: str
    detail: str = ""
    links: list[str] = field(default_factory=list)
    cost: float = 0.0

    def render(self, with_links: bool = True) -> str:
        icon = {0: "·", 1: "•", 2: "!", 3: "!!"}[self.urgency]
        out = f"{icon} {self.text}"
        if self.detail:
            out += f"\n   {self.detail}"
        if with_links and self.links:
            out += "\n   " + " ".join(self.links[:5])
        return out


class NudgeEngine:
    """All checks are independent; one failing must never hide the rest."""

    def __init__(self, settings: Settings | None = None,
                 q: query.QueryEngine | None = None):
        self.s = settings or default_settings
        self.q = q or query.QueryEngine(self.s)

    # Named so the count is quotable elsewhere without re-listing it. V
    # publishes "N vault-state checks" on its skills page, and that number
    # drifting from reality is exactly the kind of small lie that makes the
    # whole page untrustworthy.
    CHECKS = (
        "gym_gap", "gym_today", "stale_reviews", "open_work", "assignments_due",
        "empty_daily", "dormant_goals", "overdue_contacts", "birthdays",
        "schema_errors", "broken_links", "inbox_backlog", "unrated_places",
        # schema_errors returns up to two nudges; CHECKS counts checks, not
        # nudges, and the skills page quotes this length.
        "brag_gap", "brag_drafts", "stale_projects",
        "protein_shortfall", "weight_trend",
    )

    def all(self, *, today: dt.date | None = None) -> list[Nudge]:
        today = today or dt.date.today()
        checks: list[Callable[[dt.date], list[Nudge]]] = [
            getattr(self, name) for name in self.CHECKS
        ]
        out: list[Nudge] = []
        for check in checks:
            try:
                out.extend(check(today))
            except Exception as exc:  # a broken check must not kill the brief
                out.append(Nudge(f"error.{check.__name__}", NOTIFY, 0,
                                 f"nudge check '{check.__name__}' failed: {exc}"))
        out.sort(key=lambda n: -n.urgency)
        return out

    # ---- gym ---------------------------------------------------------
    def gym_gap(self, today: dt.date, threshold: int = 5) -> list[Nudge]:
        gap = self.q.gym_gap()
        if gap is None:
            return [Nudge("gym.none", NOTIFY, 1,
                          "No workouts logged in the vault at all.",
                          "04 - Gym/Workouts/ is empty — voice logging would fix that.")]
        if gap >= threshold:
            return [Nudge("gym.gap", NOTIFY, 2 if gap >= 7 else 1,
                          f"No workout logged in {gap} days.")]
        return []

    def gym_today(self, today: dt.date, after_hour: int = 19) -> list[Nudge]:
        """Ask whether today was a training day — once, and only if nothing
        has already answered it. Self-solving: the moment a workout note for
        `today` exists, logged any way, `gym_today_logged` goes true and this
        stops firing for the rest of the day. No separate state file needed.
        """
        if dt.datetime.now().hour < after_hour:
            return []
        if self.q.gym_today_logged(today):
            return []
        return [Nudge("gym.today", QUESTION, 1, "Did you train today?")]

    # ---- food / health -------------------------------------------------
    def protein_shortfall(self, today: dt.date, after_hour: int = 20,
                          threshold: float = 0.8) -> list[Nudge]:
        if dt.datetime.now().hour < after_hour:
            return []
        profile = self.q.health_profile()
        if profile is None:
            return []
        target = profile.get("protein-target-g")
        try:
            target = float(target) if target not in (None, "") else None
        except (TypeError, ValueError):
            target = None
        if not target:
            return []
        remaining = self.q.remaining_macros(today)
        if remaining is None or remaining["protein"] is None:
            return []
        left = remaining["protein"]
        consumed = target - left
        if consumed >= threshold * target:
            return []
        return [Nudge("food.protein-shortfall", NOTIFY, 1,
                      f"{consumed:.0f}g protein logged today, target "
                      f"{target:.0f}g — {left:.0f}g short with the day "
                      "almost over.")]

    def weight_trend(self, today: dt.date, days: int = 14,
                     min_gain: float = 0.1) -> list[Nudge]:
        profile = self.q.health_profile()
        if profile is None:
            return []
        goal = str(profile.get("goal") or "").strip().strip('"')
        if goal != "bulk":
            return []
        change = self.q.weight_change(days)
        if change is None or change >= min_gain:
            return []
        return [Nudge("body.weight-stall", NOTIFY, 1,
                      f"Weight is flat over the last {days} days "
                      f"({change:+.1f}kg) against a bulk goal.",
                      "worth raising calories if this holds another week or two.",
                      ["[[04 - Gym/Health Profile|Health Profile]]"])]

    # ---- work --------------------------------------------------------
    def stale_reviews(self, today: dt.date, days: int = 5) -> list[Nudge]:
        res = self.q.stale_reviews(days)
        if not res.rows:
            return []
        worst = res.rows[0]
        return [Nudge("work.stale-review", NOTIFY, 2,
                      f"{len(res.rows)} issue(s) stuck in-review.",
                      f"oldest: {worst.get('title')} — {worst.extra['days']} days",
                      [r.link() for r in res.rows])]

    def open_work(self, today: dt.date) -> list[Nudge]:
        issues = self.q.issues(status="open")
        prog = self.q.issues(status="in-progress")
        if not issues.rows and not prog.rows:
            return []
        return [Nudge("work.open", NOTIFY, 1,
                      f"{len(issues.rows)} open · {len(prog.rows)} in-progress issues.",
                      links=[r.link() for r in (prog.rows + issues.rows)[:5]])]

    # ---- university --------------------------------------------------
    def assignments_due(self, today: dt.date, within: int = 3) -> list[Nudge]:
        out = []
        for r in self.q.assignments().rows:
            due_in = r.extra.get("due_in")
            status = str(r.get("status") or "").strip('"')
            if due_in is None or status in ("submitted", "graded", "done"):
                continue
            if due_in < 0:
                out.append(Nudge("uni.overdue", NOTIFY, 3,
                                 f"'{r.get('title')}' was due {-due_in} days ago "
                                 f"and is still `{status}`.", links=[r.link()]))
            elif due_in <= within:
                body = self.q.conn.execute(
                    "SELECT words FROM notes WHERE rel=?", (r.rel,)).fetchone()
                empty = body and body["words"] < 40
                out.append(Nudge(
                    "uni.due", NOTIFY, 3 if due_in <= 1 else 2,
                    f"'{r.get('title')}' due in {due_in} day(s).",
                    "the note is still essentially empty" if empty else "",
                    [r.link()]))
        return out

    # ---- periodic ----------------------------------------------------
    def empty_daily(self, today: dt.date, after_hour: int = 21) -> list[Nudge]:
        if dt.datetime.now().hour < after_hour:
            return []
        res = self.q.daily_notes(since=today, until=today)
        if not res.rows:
            return [Nudge("daily.missing", QUESTION, 1,
                          "No daily note for today yet. Want me to scaffold it?")]
        row = self.q.conn.execute("SELECT words FROM notes WHERE rel=?",
                                  (res.rows[0].rel,)).fetchone()
        if row and row["words"] < 40:
            return [Nudge("daily.empty", QUESTION, 1,
                          "Today's note is still basically empty.",
                          "I can draft it from today's git commits.",
                          [res.rows[0].link()])]
        return []

    def dormant_goals(self, today: dt.date, days: int = 30) -> list[Nudge]:
        res = self.q.dormant_goals(days)
        if not res.rows:
            return []
        return [Nudge("goals.dormant", NOTIFY, 2,
                      f"{len(res.rows)} goal note(s) with no linked activity "
                      f"in {days} days.",
                      links=[r.link() for r in res.rows])]

    # ---- crm ---------------------------------------------------------
    def overdue_contacts(self, today: dt.date) -> list[Nudge]:
        res = self.q.overdue_contacts()
        if not res.rows:
            return []
        worst = res.rows[:3]
        detail = ", ".join(f"{r.title} ({r.extra['days_since']}d)" for r in worst)
        return [Nudge("crm.overdue", NOTIFY, 1,
                      f"{len(res.rows)} people past their contact cadence.",
                      detail, [r.link() for r in worst])]

    def birthdays(self, today: dt.date, within: int = 7) -> list[Nudge]:
        res = self.q.birthdays(within)
        out = []
        for r in res.rows:
            d = r.extra["in_days"]
            when = "today" if d == 0 else "tomorrow" if d == 1 else f"in {d} days"
            out.append(Nudge("crm.birthday", NOTIFY, 3 if d <= 1 else 1,
                             f"{r.title}'s birthday is {when}.", links=[r.link()]))
        return out

    # ---- vault hygiene ------------------------------------------------
    def schema_errors(self, today: dt.date) -> list[Nudge]:
        """Two nudges, not one, because they need different reactions.

        A note that failed to parse is broken and needs fixing. A note whose
        `status` says `opened` where the schema says `open` is fine as far as V
        is concerned — V normalises it — but Dataview compares raw strings, so
        the dashboards still miss it. Reporting them together at the same
        urgency is what produced "21 frontmatter errors" every morning, of
        which 18 were a vocabulary difference V was already handling.
        """
        report = schema.validate_vault(self.s)
        out: list[Nudge] = []

        def summarise(issues) -> str:
            by_field: dict[str, int] = {}
            for i in issues:
                by_field[i.field] = by_field.get(i.field, 0) + 1
            return ", ".join(f"{k} ({v})" for k, v in
                             sorted(by_field.items(), key=lambda kv: -kv[1])[:3])

        if report.errors:
            files = len({i.rel for i in report.errors})
            out.append(Nudge(
                "vault.schema", REVIEW, 2,
                f"{len(report.errors)} frontmatter error"
                f"{'s' if len(report.errors) != 1 else ''} across {files} "
                f"note{'s' if files != 1 else ''} — these silently vanish from "
                f"dashboards.", f"most common: {summarise(report.errors)}"))

        # Values V understands but Dataview doesn't. Lower urgency, and the
        # message says the fix is a one-off normalisation, not a hunt.
        dashboard = [i for i in report.warnings if "dashboards" in i.message]
        if dashboard:
            files = len({i.rel for i in dashboard})
            out.append(Nudge(
                "vault.dashboard-blind", REVIEW, 1,
                f"{len(dashboard)} note{'s' if len(dashboard) != 1 else ''} use "
                f"GitLab's status vocabulary, so Dataview misses "
                f"{'them' if len(dashboard) != 1 else 'it'} even though I "
                f"don't.", f"`validate --fix-status --apply` normalises all "
                           f"{files} in one pass"))
        return out

    def broken_links(self, today: dt.date, threshold: int = 1) -> list[Nudge]:
        res = self.q.broken_links()
        if len(res.rows) < threshold:
            return []
        return [Nudge("vault.broken-links", NOTIFY, 0,
                      f"{len(res.rows)} broken wikilinks.",
                      ", ".join(sorted({r.extra['broken_link']
                                        for r in res.rows})[:4]))]

    def inbox_backlog(self, today: dt.date, days: int = 3) -> list[Nudge]:
        inbox = self.q.in_folder(self.s.folders.inbox)
        if not inbox:
            return []
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
        old = []
        for r in inbox:
            row = self.q.conn.execute("SELECT mtime FROM notes WHERE rel=?",
                                      (r.rel,)).fetchone()
            if row and row["mtime"] < cutoff:
                old.append(r)
        if not old:
            return []
        return [Nudge("inbox.backlog", QUESTION, 1,
                      f"{len(old)} item(s) sitting in _Inbox for {days}+ days.",
                      links=[r.link() for r in old[:5]])]

    def unrated_places(self, today: dt.date, days: int = 7) -> list[Nudge]:
        out = []
        for r in self.q.places().rows:
            if r.get("rating") not in (None, ""):
                continue
            visited = notes.as_date(r.get("visited"))
            if visited and (today - visited).days >= days:
                out.append(r)
        if not out:
            return []
        return [Nudge("places.unrated", QUESTION, 0,
                      f"{len(out)} place(s) visited but never rated.",
                      links=[r.link() for r in out[:4]])]

    # ---- career -------------------------------------------------------
    def brag_gap(self, today: dt.date) -> list[Nudge]:
        brags = self.q.by_type("brag")
        this_month = today.strftime("%Y-%m")
        recent = [b for b in brags
                  if str(b.get("date") or "").strip('"').startswith(this_month)]
        if recent:
            return []
        merged = [m for m in self.q.merge_requests(status="merged").rows
                  if str(m.get("merged") or "").strip('"').startswith(this_month)]
        if not merged:
            return []
        return [Nudge("career.brag", REVIEW, 1,
                      f"No brag entry this month, but {len(merged)} MRs merged.",
                      "promotion evidence collected passively beats reconstructing it later",
                      [m.link() for m in merged[:3]])]

    def brag_drafts(self, today: dt.date) -> list[Nudge]:
        """Entries V drafted that still need the one line only Parsa can write.

        Urgency 0 on purpose. `briefing.signal` scores FYI nudges at zero, so
        this can never be the reason a morning gets interrupted — it rides
        along under "Worth knowing" on a morning that was already worth a push.
        A drafting feature whose whole pitch is "collect it passively" must not
        become a daily reminder to go and do work by hand.
        """
        drafts = [b for b in self.q.by_type("brag")
                  if "draft" in [str(t).lstrip("#")
                                 for t in (b.get("tags") or [])]]
        if not drafts:
            return []
        return [Nudge("career.brag-drafts", REVIEW, 0,
                      f"{len(drafts)} brag entr"
                      f"{'y' if len(drafts) == 1 else 'ies'} drafted from "
                      f"closed work, waiting on the impact line.",
                      links=[b.link() for b in drafts[:4]])]

    def stale_projects(self, today: dt.date, weeks: int = 6) -> list[Nudge]:
        cutoff = (dt.datetime.now() - dt.timedelta(weeks=weeks)).timestamp()
        out = []
        for folder in ("15 - DUNG", "16 - Leviathan", "17 - AIT",
                       "18 - TA Grader", "19 - Assistant", "20 - Melt"):
            row = self.q.conn.execute(
                "SELECT MAX(mtime) m, COUNT(*) c FROM notes WHERE rel LIKE ?",
                (folder + "/%",)).fetchone()
            if not row or not row["m"] or row["c"] == 0:
                continue
            if row["m"] < cutoff:
                age = int((dt.datetime.now().timestamp() - row["m"]) / 86400)
                out.append(Nudge("project.stale", NOTIFY, 0,
                                 f"{folder} hasn't been touched in {age} days."))
        return out


def digest(nudges: list[Nudge], *, max_items: int = 8,
           min_urgency: int = 0) -> str:
    """Render nudges as a single message — never more than one per proactive slot."""
    picked = [n for n in nudges if n.urgency >= min_urgency][:max_items]
    if not picked:
        return "Nothing needs you."
    return "\n".join(n.render() for n in picked)
