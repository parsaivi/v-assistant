"""Structured queries over the vault index. The $0 half of retrieval.

Every function here answers a question that has a schema — "which issues are
in-review?", "what did I lift last Tuesday?", "which cafés have wifi and
outdoor seating?" — with a SELECT rather than a model. They are exact, they are
free, and they cannot hallucinate a note that does not exist.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import index, notes, ui
from .config import Settings, settings as default_settings


@dataclass
class Row:
    rel: str
    title: str
    meta: dict
    extra: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        if key in self.meta:
            return self.meta[key]
        if key in self.extra:
            return self.extra[key]
        # Fall through to the row's own fields, so `title` and `rel` are
        # renderable as table columns like any frontmatter key.
        return getattr(self, key, default)

    def link(self) -> str:
        return f"[[{self.rel[:-3] if self.rel.endswith('.md') else self.rel}|{self.title}]]"


@dataclass
class Result:
    """A query answer. `rows` is the data, `answer` is the one-line rendering."""

    kind: str
    rows: list[Row]
    answer: str
    columns: tuple[str, ...] = ()
    cost: float = 0.0

    def __len__(self) -> int:
        return len(self.rows)

    def citations(self) -> list[str]:
        return [r.link() for r in self.rows]

    def table(self, limit: int = 20, cols_available: int | None = None) -> str:
        """Render as a fixed-width table that fits the terminal.

        Two things it does that the naive version didn't: it measures in
        rendered columns rather than characters, so a Persian title full of
        ZWNJ stops throwing the alignment out; and it shrinks the widest
        column until the whole table fits, instead of emitting 140 columns
        into an 80-column terminal and letting it wrap into mush.
        """
        if not self.rows:
            return "(no matches)"
        cols = self.columns or ("title",)
        rows = self.rows[:limit]
        avail = cols_available if cols_available is not None else ui.width()

        cells = {c: [_fmt(r.get(c)) for r in rows] for c in cols}

        # Squeeze the widest column repeatedly — that's the one carrying the
        # slack. When even a floor of four columns each won't fit, drop the
        # rightmost column instead: `self.columns` is ordered most-identifying
        # first, so the tail is what you can lose and still know which row is
        # which. Six unreadable columns are worth less than three legible ones.
        dropped = 0
        while True:
            widths = {c: max([ui.cell_width(c)]
                             + [ui.cell_width(v) for v in cells[c]])
                      for c in cols}
            gaps = 3 * (len(cols) - 1)      # " │ " between each pair
            while sum(widths.values()) + gaps > avail:
                widest = max(widths, key=lambda c: widths[c])
                if widths[widest] <= 4:
                    break
                widths[widest] -= 1
            if sum(widths.values()) + gaps <= avail or len(cols) == 1:
                break
            cols = cols[:-1]
            dropped += 1

        def line(values: Iterable[str]) -> str:
            return " │ ".join(ui.pad(ui.truncate(v, widths[c]), widths[c])
                              for c, v in zip(cols, values)).rstrip()

        head = line(cols)
        sep = "─┼─".join("─" * widths[c] for c in cols)
        body = "\n".join(line(vals) for vals in zip(*(cells[c] for c in cols)))
        foot = []
        if len(self.rows) > limit:
            foot.append(f"… {len(self.rows) - limit} more")
        if dropped:
            # Say so. A column that vanishes without a word is the table
            # lying about what it knows.
            foot.append(f"({dropped} column(s) hidden — the terminal is narrow)")
        tail = ("\n" + "\n".join(foot)) if foot else ""
        return f"{head}\n{sep}\n{body}{tail}"


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(i) for i in v)
    return str(v)


class QueryEngine:
    def __init__(self, settings: Settings | None = None,
                 conn: sqlite3.Connection | None = None):
        self.s = settings or default_settings
        self.conn = conn or index.connect(self.s)
        self._tasks = None
        self._calendar = None

    # ---- primitives -------------------------------------------------
    def by_type(self, type_name: str, *, include_templates: bool = False,
                **filters) -> list[Row]:
        # Templates carry a valid `type` but describe nothing real — they must
        # never appear in a count, a dashboard, or a nudge.
        sql = "SELECT rel, title, meta FROM notes WHERE type=?"
        params: list = [type_name]
        if not include_templates:
            sql += " AND rel NOT LIKE ?"
            params.append(self.s.folders.templates + "/%")
        rows = [self._row(r) for r in self.conn.execute(sql, params)]
        return [r for r in rows if _matches(r.meta, filters)]

    def in_folder(self, folder: str) -> list[Row]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT rel, title, meta FROM notes WHERE rel LIKE ? ORDER BY rel",
            (folder.rstrip("/") + "/%",))]

    def _row(self, r: sqlite3.Row) -> Row:
        return Row(r["rel"], r["title"], json.loads(r["meta"] or "{}"))

    # ---- work -------------------------------------------------------
    def issues(self, status: str | None = None, project: str | None = None,
               sprint: str | None = None) -> Result:
        rows = self.by_type("issue")
        if status:
            want = normalise_status(status)
            rows = [r for r in rows if normalise_status(r.get("status")) == want]
        if project:
            rows = [r for r in rows if _s(r.get("project")).lower() == project.lower()]
        if sprint:
            rows = [r for r in rows if _s(r.get("sprint")) == sprint]
        rows.sort(key=lambda r: (_PRIORITY.get(_s(r.get("priority")), 9),
                                 str(r.get("created") or "")), reverse=False)
        label = f"{status} " if status else ""
        return Result("issues", rows,
                      f"{len(rows)} {label}issue{'s' if len(rows) != 1 else ''}"
                      + (f" in {project}" if project else ""),
                      ("issue-id", "title", "status", "priority", "project"))

    def merge_requests(self, status: str | None = None,
                       project: str | None = None) -> Result:
        rows = self.by_type("merge-request")
        if status:
            want = normalise_status(status)
            rows = [r for r in rows if normalise_status(r.get("status")) == want]
        if project:
            rows = [r for r in rows if _s(r.get("project")).lower() == project.lower()]
        rows.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
        return Result("merge-requests", rows,
                      f"{len(rows)} {status + ' ' if status else ''}MRs",
                      ("mr-id", "title", "status", "project"))

    def stale_reviews(self, days: int = 5) -> Result:
        """Issues sitting in-review longer than `days` — a $0 nudge source."""
        today = dt.date.today()
        out = []
        for r in self.by_type("issue"):
            if normalise_status(r.get("status")) != "in-review":
                continue
            created = notes.as_date(r.get("created"))
            age = (today - created).days if created else None
            if age is not None and age >= days:
                r.extra["days"] = age
                out.append(r)
        out.sort(key=lambda r: -r.extra["days"])
        return Result("stale-reviews", out,
                      f"{len(out)} issues in-review for {days}+ days",
                      ("issue-id", "title", "days", "project"))

    def blocked(self) -> Result:
        """Everything waiting on someone else, or on you."""
        rows = [r for r in self.by_type("issue")
                if normalise_status(r.get("status")) in ("in-review", "in-progress")]
        rows += [r for r in self.by_type("merge-request")
                 if normalise_status(r.get("status")) == "open"]
        return Result("blocked", rows, f"{len(rows)} open work items",
                      ("title", "status", "project"))

    # ---- gym --------------------------------------------------------
    def workouts(self, since: dt.date | None = None,
                 training_type: str | None = None) -> Result:
        rows = self.by_type("workout")
        out = []
        for r in rows:
            d = notes.as_date(r.get("date")) or notes.as_date(r.rel.split("/")[-1][:10])
            if d is None:
                continue
            if since and d < since:
                continue
            if training_type and training_type.lower() not in _s(
                    r.get("type-of-training")).lower():
                continue
            r.extra["date"] = d.isoformat()
            out.append(r)
        out.sort(key=lambda r: r.extra["date"], reverse=True)
        return Result("workouts", out, f"{len(out)} workouts",
                      ("date", "type-of-training", "duration", "energy-level"))

    def last_workout(self, training_type: str | None = None) -> Result:
        res = self.workouts(training_type=training_type)
        rows = res.rows[:1]
        if not rows:
            return Result("workouts", [], "no workouts logged yet",
                          ("date", "type-of-training"))
        d = rows[0].extra["date"]
        gap = (dt.date.today() - dt.date.fromisoformat(d)).days
        return Result("workouts", rows,
                      f"last workout {d} ({gap} day{'s' if gap != 1 else ''} ago)",
                      ("date", "type-of-training", "duration", "energy-level"))

    def gym_gap(self) -> int | None:
        res = self.workouts()
        if not res.rows:
            return None
        return (dt.date.today() - dt.date.fromisoformat(res.rows[0].extra["date"])).days

    def gym_today_logged(self, day: dt.date) -> bool:
        """Whether a workout note already exists for `day` — the check that
        keeps a gym nudge from asking twice in one day (see nudges.gym_today).
        """
        for r in self.by_type("workout"):
            d = notes.as_date(r.get("date")) or notes.as_date(r.rel.split("/")[-1][:10])
            if d == day:
                return True
        return False

    # ---- food / health ------------------------------------------------
    def meals(self, since: dt.date | None = None, until: dt.date | None = None,
              meal: str | None = None) -> Result:
        rows = self.by_type("meal")
        out = []
        for r in rows:
            d = notes.as_date(r.get("date"))
            if d is None:
                continue
            if since and d < since:
                continue
            if until and d > until:
                continue
            if meal and _s(r.get("meal")).lower() != meal.lower():
                continue
            r.extra["date"] = d.isoformat()
            out.append(r)
        out.sort(key=lambda r: r.extra["date"], reverse=True)
        return Result("meals", out, f"{len(out)} meals",
                      ("date", "meal", "calories", "protein", "carbs", "fiber"))

    def macro_totals(self, day: dt.date) -> dict:
        """Sum of calories/protein/carbs/fiber logged for a single day."""
        totals = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fiber": 0.0}
        for r in self.meals(since=day, until=day).rows:
            for k in totals:
                v = r.get(k)
                try:
                    totals[k] += float(v) if v not in (None, "") else 0.0
                except (TypeError, ValueError):
                    continue
        return totals

    def health_profile(self) -> Row | None:
        rows = self.by_type("health-profile")
        return rows[0] if rows else None

    def remaining_macros(self, day: dt.date) -> dict | None:
        """Target minus logged-so-far, per macro. `None` if there's no Health
        Profile note yet — there is nothing to compare against."""
        profile = self.health_profile()
        if profile is None:
            return None
        totals = self.macro_totals(day)
        out: dict = {}
        for key, target_key in (("calories", "calorie-target"),
                                ("protein", "protein-target-g"),
                                ("carbs", "carbs-target-g"),
                                ("fiber", "fiber-target-g")):
            target = profile.get(target_key)
            try:
                target = float(target) if target not in (None, "") else None
            except (TypeError, ValueError):
                target = None
            out[key] = None if target is None else target - totals[key]
        return out

    def _food_decision_note(self) -> notes.Note | None:
        path = self.s.vault / self.s.folders.gym / "Food Decision Log.md"
        if not path.exists():
            return None
        try:
            return notes.parse(path, self.s.vault)
        except (OSError, UnicodeDecodeError):
            return None

    def _bulleted(self, heading: str) -> list[str]:
        note = self._food_decision_note()
        if note is None:
            return []
        section = note.section(heading) or ""
        out = []
        for line in section.splitlines():
            line = line.strip()
            if line.startswith("- "):
                item = line[2:].strip()
                if item:
                    out.append(item)
        return out

    def food_likes(self) -> list[str]:
        """The `## Likes` bullets from `04 - Gym/Food Decision Log.md`."""
        return self._bulleted("Likes")

    def food_dislikes(self) -> list[str]:
        """The `## Dislikes` bullets from `04 - Gym/Food Decision Log.md`."""
        return self._bulleted("Dislikes")

    def bodyweight_trend(self, days: int = 14) -> Result:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        out = []
        for r in self.by_type("bodyweight"):
            d = notes.as_date(r.get("date"))
            if d is None or d < cutoff:
                continue
            r.extra["date"] = d.isoformat()
            out.append(r)
        out.sort(key=lambda r: r.extra["date"])
        return Result("bodyweight", out, f"{len(out)} weigh-ins in {days} days",
                      ("date", "weight-kg"))

    def weight_change(self, days: int = 14) -> float | None:
        """Last weigh-in minus first, over the window. `None` under 2 points."""
        rows = self.bodyweight_trend(days).rows
        if len(rows) < 2:
            return None
        try:
            first = float(rows[0].get("weight-kg"))
            last = float(rows[-1].get("weight-kg"))
        except (TypeError, ValueError):
            return None
        return last - first

    # ---- finance ------------------------------------------------------
    def category_spend(self, jalali_month: str | None = None
                       ) -> tuple[str, dict[str, float]] | None:
        """Category totals (toman) from a `Category Snapshot` note.

        `jalali_month` picks an exact note (`"1405-05"`); otherwise the
        lexicographically-latest one, which is chronological because the
        field is always zero-padded `YYYY-MM`. Categories are read straight
        off whatever `-toman` keys the note's frontmatter actually has,
        rather than against the taxonomy hardcoded in `schema.py` — so a
        category added there does not silently need a matching edit here.
        `None` if there is no snapshot note yet (nothing to chart).
        """
        rows = self.by_type("category-snapshot")
        if jalali_month:
            rows = [r for r in rows if _s(r.get("jalali-month")) == jalali_month]
        else:
            rows = sorted(rows, key=lambda r: _s(r.get("jalali-month")))
        if not rows:
            return None
        row = rows[-1]
        totals: dict[str, float] = {}
        for key, val in row.meta.items():
            if not key.endswith("-toman"):
                continue
            try:
                amount = float(val) if val not in (None, "") else 0.0
            except (TypeError, ValueError):
                continue
            if amount:
                totals[key[: -len("-toman")]] = amount
        totals = dict(sorted(totals.items(), key=lambda kv: -kv[1]))
        return _s(row.get("jalali-month")), totals

    # ---- places -----------------------------------------------------
    def resolve_anchor(self, near: str) -> tuple[float, float, str] | None:
        """"Home", "Divar", a note title, or a raw "lat,lng" — to coordinates.

        Tried in that order: a place note is what "near X" almost always
        means (the vault already has `Home.md`/`Divar.md`), and only falls
        back to parsing `near` itself as coordinates so a caller that already
        has a lat/lng (e.g. from a live map search hit) can skip resolution.
        Returns `None` rather than raising — "near" is user-typed free text
        and a typo is a normal, answerable-with-an-explanation outcome, not
        an exception.
        """
        near_l = (near or "").strip().lower()
        if not near_l:
            return None
        places = self.by_type("place")
        for r in places:
            if r.title.strip().lower() == near_l:
                loc = _parse_latlng(r.get("location"))
                if loc:
                    return (loc[0], loc[1], r.title)
        for r in places:
            if near_l in r.title.strip().lower():
                loc = _parse_latlng(r.get("location"))
                if loc:
                    return (loc[0], loc[1], r.title)
        coord = _parse_latlng(near)
        if coord:
            return (coord[0], coord[1], near.strip())
        return None

    def places(self, category: str | None = None, min_rating: int | None = None,
               wifi: bool | None = None, outdoor: bool | None = None,
               open_late: bool | None = None, city: str | None = None,
               good_for: str | None = None, near: str | None = None,
               radius_km: float | None = None) -> Result:
        anchor = None
        if near:
            anchor = self.resolve_anchor(near)
            if anchor is None:
                return Result(
                    "places", [],
                    f"Don't know where {near!r} is — no place note by that "
                    "name and it doesn't parse as \"lat,lng\" either.",
                    ("name", "category", "rating", "neighborhood", "noise"))
        rows = self.by_type("place")
        out, unknown = [], 0
        for r in rows:
            if category and _s(r.get("category")).lower() != category.lower():
                continue
            if min_rating is not None:
                try:
                    if float(r.get("rating") or 0) < min_rating:
                        continue
                except (TypeError, ValueError):
                    continue
            # An unrecorded flag excludes nothing. Dropping a café because its
            # `wifi:` line is blank answers "0 places" to a question the vault
            # can partly answer, and the blank is a gap in the note rather than
            # a statement about the café. Only a recorded contradiction filters.
            missing = False
            for flag, want in (("wifi", wifi), ("outdoor-seating", outdoor),
                               ("open-late", open_late)):
                if want is None:
                    continue
                have = _tri(r.get(flag))
                if have is None:
                    missing = True
                elif have != want:
                    break
            else:
                if city and _s(r.get("city")).lower() != city.lower():
                    continue
                if good_for and good_for.lower() not in _s(r.get("good-for")).lower():
                    continue
                if missing:
                    unknown += 1
                    r.extra["unverified"] = True
                out.append(r)

        cols = ("name", "category", "rating", "neighborhood", "noise")
        if anchor is not None:
            lat, lng, label = anchor
            # A place with no recorded location cannot be ranked by distance,
            # and "cafés near Home" silently including one at an unknown
            # distance would be worse than dropping it — it would sort in at
            # whatever position float(None) happens to land on.
            near_out = []
            for r in out:
                loc = _parse_latlng(r.get("location"))
                if loc is None:
                    continue
                r.extra["distance_km"] = round(haversine_km(lat, lng, *loc), 1)
                if radius_km is not None and r.extra["distance_km"] > radius_km:
                    continue
                near_out.append(r)
            out = near_out
            out.sort(key=lambda r: r.extra["distance_km"])
            cols = ("name", "distance_km", "category", "rating", "neighborhood")
            answer = f"{len(out)} places"
            if radius_km is not None:
                answer += f" within {radius_km}km"
            answer += f" of {label}"
        else:
            out.sort(key=lambda r: (bool(r.extra.get("unverified")),
                                    -(float(r.get("rating") or 0))))
            answer = f"{len(out)} places"
            if unknown:
                answer += f" ({unknown} with that detail not recorded)"
        return Result("places", out, answer, cols)

    # ---- crm --------------------------------------------------------
    def people(self) -> Result:
        rows = self.by_type("person")
        return Result("people", rows, f"{len(rows)} people",
                      ("title", "relation_type", "tier", "last_contact"))

    def overdue_contacts(self) -> Result:
        """People you're past due to contact, per their own `frequency_days`."""
        today = dt.date.today()
        out = []
        for r in self.by_type("person"):
            last = notes.as_date(r.get("last_contact"))
            freq = r.get("frequency_days")
            if not last or not freq:
                continue
            try:
                freq = int(freq)
            except (TypeError, ValueError):
                continue
            overdue = (today - last).days - freq
            if overdue > 0:
                r.extra["days_since"] = (today - last).days
                r.extra["overdue_by"] = overdue
                out.append(r)
        out.sort(key=lambda r: -r.extra["overdue_by"])
        return Result("overdue-contacts", out,
                      f"{len(out)} people overdue for contact",
                      ("title", "days_since", "overdue_by", "tier"))

    def birthdays(self, within_days: int = 14) -> Result:
        today = dt.date.today()
        out = []
        for r in self.by_type("person"):
            b = notes.as_date(r.get("birthday"))
            if not b:
                continue
            nxt = b.replace(year=today.year)
            if nxt < today:
                nxt = b.replace(year=today.year + 1)
            delta = (nxt - today).days
            if delta <= within_days:
                r.extra["in_days"] = delta
                r.extra["on"] = nxt.isoformat()
                out.append(r)
        out.sort(key=lambda r: r.extra["in_days"])
        return Result("birthdays", out, f"{len(out)} birthdays in {within_days} days",
                      ("title", "on", "in_days"))

    # ---- university -------------------------------------------------
    def assignments(self, status: str | None = None,
                    due_within: int | None = None) -> Result:
        today = dt.date.today()
        out = []
        for r in self.by_type("assignment"):
            if status and normalise_status(r.get("status")) != normalise_status(status):
                continue
            due = notes.as_date(r.get("due-date"))
            if due:
                r.extra["due_in"] = (due - today).days
            if due_within is not None:
                if due is None or not (0 <= (due - today).days <= due_within):
                    continue
            out.append(r)
        out.sort(key=lambda r: r.extra.get("due_in", 9999))
        return Result("assignments", out, f"{len(out)} assignments",
                      ("title", "course", "due-date", "due_in", "status"))

    # ---- periodic ---------------------------------------------------
    def daily_notes(self, since: dt.date | None = None,
                    until: dt.date | None = None) -> Result:
        out = []
        for r in self.in_folder(self.s.folders.daily):
            if "/Weekly/" in r.rel or "/Monthly/" in r.rel:
                continue
            d = notes.as_date(r.get("date")) or notes.as_date(r.rel.split("/")[-1][:10])
            if not d:
                continue
            if since and d < since:
                continue
            if until and d > until:
                continue
            r.extra["date"] = d.isoformat()
            out.append(r)
        out.sort(key=lambda r: r.extra["date"], reverse=True)
        return Result("daily-notes", out, f"{len(out)} daily notes",
                      ("date", "week", "mood", "energy"))

    def on_this_day(self, years_back: int = 1) -> Result:
        today = dt.date.today()
        try:
            target = today.replace(year=today.year - years_back)
        except ValueError:
            target = today.replace(year=today.year - years_back, day=28)
        rows = [r for r in self.daily_notes().rows if r.extra["date"] == target.isoformat()]
        return Result("on-this-day", rows,
                      f"{len(rows)} notes from {target.isoformat()}", ("date",))

    def goals(self, quarter: str | None = None) -> Result:
        rows = self.in_folder(self.s.folders.goals)
        if quarter:
            rows = [r for r in rows if quarter in r.rel]
        return Result("goals", rows, f"{len(rows)} goal notes", ("title", "status"))

    def dormant_goals(self, days: int = 30) -> Result:
        """Quarterly objectives with no note linking to them recently."""
        cutoff = dt.date.today() - dt.timedelta(days=days)
        out = []
        for g in self.goals().rows:
            stem = g.rel[:-3]
            backlinks = [r["src"] for r in self.conn.execute(
                "SELECT src FROM links WHERE dst=? OR dst=?",
                (stem, stem.split("/")[-1]))]
            recent = 0
            for src in backlinks:
                row = self.conn.execute(
                    "SELECT mtime FROM notes WHERE rel=?", (src,)).fetchone()
                if row and dt.date.fromtimestamp(row["mtime"]) >= cutoff:
                    recent += 1
            if recent == 0:
                g.extra["backlinks"] = len(backlinks)
                out.append(g)
        return Result("dormant-goals", out,
                      f"{len(out)} goal notes with no activity in {days} days",
                      ("title", "backlinks"))

    # ---- vault hygiene ----------------------------------------------
    def orphans(self) -> Result:
        """Notes nothing links to. Candidates for an Atlas MOC entry."""
        linked = {r["dst"] for r in self.conn.execute("SELECT DISTINCT dst FROM links")}
        linked |= {d.split("/")[-1] for d in linked}
        out = []
        for r in self.conn.execute("SELECT rel, title, meta FROM notes"):
            row = self._row(r)
            stem = row.rel[:-3]
            if stem in linked or stem.split("/")[-1] in linked:
                continue
            if any(row.rel.startswith(p) for p in (
                    self.s.folders.templates, self.s.folders.daily,
                    self.s.folders.archive)):
                continue
            out.append(row)
        return Result("orphans", out, f"{len(out)} orphan notes", ("title",))

    def broken_links(self) -> Result:
        """Wikilinks pointing at notes that do not exist."""
        stems = {r["rel"][:-3] for r in self.conn.execute("SELECT rel FROM notes")}
        names = {s.split("/")[-1] for s in stems}
        aliases: set[str] = set()
        for r in self.conn.execute("SELECT meta FROM notes"):
            al = json.loads(r["meta"] or "{}").get("aliases") or []
            if isinstance(al, list):
                aliases |= {str(a) for a in al}
        folders = {s.rsplit("/", 1)[0] for s in stems}
        out = []
        for r in self.conn.execute("SELECT src, dst FROM links ORDER BY src"):
            dst = r["dst"].strip()
            if dst in stems or dst in names or dst in aliases:
                continue
            if dst.rstrip("/") in folders:
                continue  # a folder link
            # `![[diagram.png]]` is an attachment embed, not a note reference.
            # Counting those as broken links buries the real ones in noise.
            if ATTACHMENT_RE.search(dst):
                continue
            out.append(Row(r["src"], r["src"].split("/")[-1][:-3],
                           {}, {"broken_link": dst}))
        return Result("broken-links", out, f"{len(out)} broken wikilinks",
                      ("title", "broken_link"))

    def untyped(self) -> Result:
        rows = [self._row(r) for r in self.conn.execute(
            "SELECT rel, title, meta FROM notes WHERE type IS NULL"
            " AND rel NOT LIKE '01 - Daily Notes/%'"
            " AND rel NOT LIKE '05 - Templates/%'")]
        return Result("untyped", rows, f"{len(rows)} notes with no `type`", ("title",))

    def empty_notes(self, max_words: int = 15) -> Result:
        rows = [self._row(r) for r in self.conn.execute(
            "SELECT rel, title, meta FROM notes WHERE words < ?"
            " AND rel NOT LIKE '05 - Templates/%' ORDER BY words", (max_words,))]
        return Result("empty", rows, f"{len(rows)} near-empty notes", ("title",))

    # ---- direct lookup -----------------------------------------------
    def by_identifier(self, text: str) -> Result:
        """Resolve "ISSUE-139", "MR-747" or "[[note name]]" to the note itself.

        Naming a note is the most direct question there is, and it used to be
        the one the router couldn't answer — it scored nothing, landed in the
        ambiguity prompt, and offered to file *ISSUE-139* as a lyric.
        """
        raw = (text or "").strip().strip("?").strip()
        wiki = re.match(r"^\[\[([^\]|#]+)", raw)
        if wiki:
            stem = wiki.group(1).strip()
            rows = [self._row(r) for r in self.conn.execute(
                "SELECT rel, title, meta FROM notes"
                " WHERE rel = ? OR rel LIKE ?", (stem + ".md", f"%/{stem}.md"))]
            return Result("note", rows,
                          f"{len(rows)} note matching [[{stem}]]"
                          if rows else f"No note called [[{stem}]].",
                          ("title", "rel"))

        m = re.match(r"^(issue|mr|merge[\s-]?request|sprint)[\s-]?#?(\d+)$",
                     raw, re.I)
        if not m:
            return Result("note", [], "not an identifier", ("title",))
        kind, num = m.group(1).lower(), m.group(2)
        type_name = "issue" if kind == "issue" else (
            "sprint" if kind == "sprint" else "merge-request")
        field = {"issue": "issue-id", "merge-request": "mr-id",
                 "sprint": "sprint-number"}[type_name]
        rows = [r for r in self.by_type(type_name)
                if _s(r.get(field)) == num]
        label = raw.upper()
        if not rows:
            return Result(type_name, [], f"No {label} in the vault.",
                          ("title",))
        cols = (("issue-id", "title", "status", "priority", "project")
                if type_name == "issue"
                else ("mr-id", "title", "status", "project"))
        return Result(type_name, rows, f"{label} — {rows[0].title}", cols)

    # ---- tasks (checkboxes, still $0) --------------------------------
    @property
    def tasks(self):
        if self._tasks is None:
            from .tasks import TaskStore
            self._tasks = TaskStore(self.conn, self.s)
        return self._tasks

    def _task_result(self, kind: str, rows, answer: str, *,
                     today: dt.date | None = None) -> Result:
        out = [Row(t.rel, t.text, {}, {
            "task": t.render(with_source=False, today=today),
            "where": t.section or t.rel.rsplit("/", 1)[-1][:-3],
            "area": t.area or "—",
            "due": t.due.isoformat() if t.due else (
                f"{t.age_days(today)}d old" if (t.age_days(today) or 0) > 0
                else "—"),
            "id": t.id,
            "_task": t,
        }) for t in rows]
        return Result(kind, out, answer, ("task", "where", "area", "due"))

    def open_tasks(self, area: str | None = None, limit: int = 25,
                   folder: str | None = None) -> Result:
        from .tasks import collapse
        rows = collapse(self.tasks.open(area=area, folder=folder or None))
        rows.sort(key=lambda t: -t.urgency())
        label = f" in {area}" if area else (
            f" from {folder}" if folder else "")
        return self._task_result(
            "tasks", rows[:limit],
            f"{len(rows)} open task{'s' if len(rows) != 1 else ''}{label}")

    def priorities(self, today: dt.date | None = None, limit: int = 8,
                   area: str | None = None) -> Result:
        today = today or dt.date.today()
        rows = self.tasks.priorities(today=today, limit=limit, area=area)
        when = ("today" if today == dt.date.today()
                else "tomorrow" if today == dt.date.today() + dt.timedelta(1)
                else today.isoformat())
        return self._task_result(
            "priorities", rows,
            f"Top {len(rows)} for {when}" if rows
            else "Nothing open — the vault has no unfinished tasks.",
            today=today)

    def overdue_tasks(self, today: dt.date | None = None,
                      limit: int = 20) -> Result:
        rows = self.tasks.overdue(today)
        return self._task_result(
            "overdue", rows[:limit],
            f"{len(rows)} task{'s' if len(rows) != 1 else ''} past their date",
            today=today)

    def find_task(self, text: str, *, open_only: bool = True) -> list:
        return self.tasks.find(text, open_only=open_only)

    def backlog(self, today: dt.date | None = None) -> Result:
        """The answer to "how far behind am I" — counted, with the arithmetic
        visible so the number can be argued with."""
        today = today or dt.date.today()
        s = self.tasks.backlog_shape(today)
        issues = [r for r in self.by_type("issue")
                  if normalise_status(r.get("status")) in ("open", "in-progress",
                                                           "in-review")]
        mrs = [r for r in self.by_type("merge-request")
               if normalise_status(r.get("status")) == "open"]
        rows = [
            Row("", "open tasks", {}, {"metric": "open tasks",
                                       "value": s["open"]}),
            Row("", "past their date", {}, {"metric": "past their date",
                                            "value": s["overdue"]}),
            Row("", "live (≤3 weeks)", {}, {"metric": "live (≤3 weeks)",
                                            "value": s["live"]}),
            Row("", "rotting (>2 months)", {}, {"metric": "rotting (>2 months)",
                                                "value": s["rotting"]}),
            Row("", "oldest", {}, {"metric": "oldest",
                                   "value": f"{s['oldest_days']}d"}),
            Row("", "closed last 7 days", {}, {"metric": "closed last 7 days",
                                               "value": s["closed_last_7"]}),
            Row("", "open issues", {}, {"metric": "open issues",
                                        "value": len(issues)}),
            Row("", "open MRs", {}, {"metric": "open MRs", "value": len(mrs)}),
        ]
        verdict = _backlog_verdict(s, len(issues), len(mrs))
        return Result("backlog", rows, verdict, ("metric", "value"))

    # ---- calendar ----------------------------------------------------
    @property
    def calendar(self):
        if self._calendar is None:
            from .calendar import Calendar
            self._calendar = Calendar(self.s)
        return self._calendar

    def events(self, day: dt.date | None = None, *, days: int = 1) -> Result:
        day = day or dt.date.today()
        end = day + dt.timedelta(days=days - 1)
        evs = self.calendar.between(day, end)
        rows = [Row(self.calendar.month_rel(e.date),
                    e.summary, {}, {"when": e.time_label,
                                    "date": e.date.isoformat(),
                                    "what": e.summary,
                                    "where": e.location or "—"})
                for e in evs]
        if days == 1:
            label = ("today" if day == dt.date.today()
                     else "tomorrow" if day == dt.date.today() + dt.timedelta(1)
                     else day.isoformat())
            answer = (f"{len(evs)} event{'s' if len(evs) != 1 else ''} {label}"
                      if evs else f"Nothing on the calendar for {label}.")
        else:
            answer = (f"{len(evs)} events between {day} and {end}" if evs
                      else f"Nothing scheduled between {day} and {end}.")
        return Result("events", rows, answer, ("date", "when", "what", "where"))

    # ---- keyword search (FTS, still $0) ------------------------------
    def search(self, text: str, limit: int = 10) -> Result:
        if index.has_fts(self.conn):
            try:
                q = " OR ".join(f'"{w}"' for w in text.split() if len(w) > 1)
                rows = self.conn.execute(
                    "SELECT n.rel, n.title, n.meta,"
                    " snippet(notes_fts, 2, '«', '»', '…', 14) AS snip"
                    " FROM notes_fts f JOIN notes n ON n.rel = f.rel"
                    " WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?",
                    (q, limit)).fetchall()
                out = [Row(r["rel"], r["title"], json.loads(r["meta"] or "{}"),
                           {"snippet": r["snip"]}) for r in rows]
                return Result("search", out, f"{len(out)} keyword matches",
                              ("title", "snippet"))
            except sqlite3.OperationalError:
                pass
        like = f"%{text}%"
        rows = self.conn.execute(
            "SELECT rel, title, meta FROM notes WHERE body LIKE ? OR title LIKE ?"
            " LIMIT ?", (like, like, limit)).fetchall()
        out = [self._row(r) for r in rows]
        return Result("search", out, f"{len(out)} matches", ("title",))

    def close(self) -> None:
        self.conn.close()


def _backlog_verdict(s: dict, issues: int, mrs: int) -> str:
    """One honest sentence about where things stand.

    Says the number *and* what it means, because "124 overdue" reads as a
    catastrophe when 114 of those are this month's work and 10 are genuinely
    abandoned. A backlog answer that only counts is a backlog answer that
    always sounds the same.
    """
    parts = [f"{s['open']} open tasks, {s['overdue']} past their date"]
    if s["rotting"]:
        parts.append(f"{s['rotting']} untouched for over two months")
    if s["carried"]:
        parts.append(f"{s['carried']} you've rewritten three days or more")
    parts.append(f"{issues} open issues, {mrs} open MRs")
    parts.append(f"{s['closed_last_7']} closed in the last week")
    return " · ".join(parts)


_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}

ATTACHMENT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|pdf|mp4|mov|mp3|wav|ogg|m4a|zip|xlsx?|docx?|"
    r"pptx?|canvas|excalidraw)$", re.I)

# GitLab says `opened`; CLAUDE.md's schema says `open`; a human types either.
# Comparing raw strings meant `issues(status="open")` matched nothing at all
# across 40 issues and 181 MRs — and answered "0 open issues", which reads like
# a fact rather than a vocabulary mismatch. Normalise BOTH sides of every status
# comparison instead of migrating the notes: the notes are re-synced from
# GitLab, so a migration only stays fixed until the next `work --apply`.
_STATUS_ALIASES = {
    "opened": "open", "open": "open", "reopened": "open",
    "closed": "closed", "close": "closed",
    "merged": "merged",
    "in review": "in-review", "in-review": "in-review", "inreview": "in-review",
    "review": "in-review", "needs review": "in-review",
    "in progress": "in-progress", "in-progress": "in-progress",
    "inprogress": "in-progress", "doing": "in-progress", "wip": "in-progress",
    "todo": "open", "backlog": "open",
    "done": "closed", "complete": "closed", "completed": "closed",
    "locked": "closed",
}


def normalise_status(v: Any) -> str:
    """Map a status value onto the schema's vocabulary. Unknown values pass
    through lowercased so a typo is still comparable to itself."""
    s = _s(v).lower()
    return _STATUS_ALIASES.get(s, s)


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip().strip('"')


_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. Fine at Tehran-neighborhood scale, where
    the ~0.3% error against an ellipsoidal model is smaller than GPS noise."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def _parse_latlng(v: Any) -> tuple[float, float] | None:
    """The vault's `location` field: plain `"lat,lng"` (current convention)
    or the older `[lat, lng]` array Map View also still accepts."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return float(v[0]), float(v[1])
        except (TypeError, ValueError):
            return None
    s = _s(v).strip("[]() ")
    if not s or "," not in s:
        return None
    lat_s, _, lng_s = s.partition(",")
    try:
        return float(lat_s.strip()), float(lng_s.strip())
    except ValueError:
        return None


# Blank is not False. `wifi:` with no value means "never recorded", and folding
# that to False made "cafés with wifi" silently exclude every café whose wifi
# field was empty — while `wifi: good`, the vocabulary the vault actually uses,
# was excluded too for not being the literal string "true".
_TRUE_WORDS = ("true", "yes", "1", "y", "good", "great", "fast", "strong",
               "excellent", "ok", "okay", "decent", "available", "بله", "دارد")
_FALSE_WORDS = ("false", "no", "0", "n", "none", "bad", "poor", "unusable",
                "خیر", "ندارد")


def _tri(v: Any) -> bool | None:
    """True / False / None-for-unknown."""
    if isinstance(v, bool):
        return v
    s = _s(v).lower()
    if not s:
        return None
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return None


def _truthy(v: Any) -> bool:
    return _tri(v) is True


def _matches(meta: dict, filters: dict) -> bool:
    for k, want in filters.items():
        if want is None:
            continue
        if _s(meta.get(k)).lower() != str(want).lower():
            return False
    return True
