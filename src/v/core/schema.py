"""Frontmatter schemas from CLAUDE.md, and a validator for them.

Why this exists: a typo in `status` silently drops a note from every Dataview
dashboard. Nothing errors, nothing warns — the note just stops existing as far
as the dashboards are concerned. This is a linter for that failure mode.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import notes
from .config import Settings, settings as default_settings

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Field:
    name: str
    required: bool = False
    enum: tuple[str, ...] | None = None
    kind: str | None = None  # date | int | float | bool | list | str
    note: str = ""


@dataclass
class Schema:
    type: str
    folder: str | None
    fields: list[Field]
    filename: Callable[[str], bool] | None = None
    filename_hint: str = ""

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


SCHEMAS: dict[str, Schema] = {
    "issue": Schema("issue", "02 - Work/Issues", [
        Field("issue-id", True, kind="int"),
        Field("title", True),
        Field("project", True),
        Field("status", True, enum=("open", "in-progress", "in-review", "closed")),
        Field("priority", True, enum=("critical", "high", "medium", "low")),
        Field("estimate"), Field("sprint"), Field("assignee"),
        Field("created", True, kind="date"), Field("closed", kind="date"),
        Field("gitlab-url"),
    ]),
    "merge-request": Schema("merge-request", "02 - Work/Merge Requests", [
        Field("mr-id", True, kind="int"),
        Field("title", True), Field("project", True),
        Field("status", True, enum=("open", "merged", "closed")),
        Field("created", True, kind="date"), Field("merged", kind="date"),
        Field("branch"), Field("target-branch"), Field("reviewer"),
        Field("gitlab-url"),
    ]),
    "workout": Schema("workout", "04 - Gym/Workouts", [
        Field("date", True, kind="date"),
        Field("type-of-training", True),
        Field("duration"), Field("energy-level"),
    ], filename_hint="YYYY-MM-DD <Type>.md"),
    "place": Schema("place", "09 - Anti-Library/05 - Locations", [
        Field("location", True, note="lat,lng plain string — read by Map View"),
        Field("category", True,
              enum=("cafe", "restaurant", "park", "bar", "bookstore", "other")),
        Field("neighborhood"), Field("city"),
        Field("rating", kind="int"),
        Field("price", enum=("cheap", "medium", "expensive")),
        Field("visited", kind="date"), Field("good-for"),
        Field("wifi"), Field("noise"), Field("outdoor-seating"), Field("open-late"),
    ]),
    "sprint": Schema("sprint", "02 - Work/Sprints", [
        Field("sprint-number", True), Field("start", True, kind="date"),
        Field("end", True, kind="date"), Field("goal"), Field("status"),
    ]),
    "assignment": Schema("assignment", "03 - University/Assignments", [
        Field("title", True), Field("course", True),
        Field("due-date", True, kind="date"),
        Field("status", True, enum=("todo", "in-progress", "submitted", "graded", "done")),
        Field("priority", enum=("critical", "high", "medium", "low")),
        Field("submitted", kind="date"), Field("grade"),
    ]),
    "course": Schema("course", "03 - University/Courses", [
        Field("title", True), Field("professor"), Field("semester"),
        Field("credits"), Field("status", enum=("active", "completed", "dropped")),
    ]),
    "person": Schema("person", "10 - Personal CRM", [
        Field("relation_type"), Field("tier", kind="int"),
        Field("frequency_days", kind="int"),
        Field("last_contact", kind="date"), Field("birthday", kind="date"),
        Field("company"), Field("role"), Field("location"),
        # `core/contacts.py` reads these two out of the body as well, since
        # most of the folder predates them being fields at all. Declaring
        # them here is what makes the frontmatter the place to put one.
        Field("email"), Field("phone"),
    ]),
    "health-profile": Schema("health-profile", "04 - Gym", [
        Field("height-cm", True, kind="float"),
        Field("age", True, kind="int"),
        Field("weight-start-kg", True, kind="float"),
        Field("goal", True, enum=("bulk", "cut", "maintain")),
        Field("calorie-target", kind="int"),
        Field("protein-target-g", kind="int"),
        Field("carbs-target-g", kind="int"),
        Field("fiber-target-g", kind="int"),
    ], filename_hint="Health Profile.md"),
    "meal": Schema("meal", "04 - Gym/Food", [
        Field("date", True, kind="date"),
        Field("meal", True, enum=("breakfast", "lunch", "dinner", "snack")),
        Field("calories", kind="int"), Field("protein", kind="int"),
        Field("carbs", kind="int"), Field("fiber", kind="int"),
        # Set when V estimated these from a food description rather than
        # Parsa stating them — keeps estimated and stated entries tellable
        # apart later, since only one of them is trustworthy at face value.
        Field("estimated", kind="bool"),
    ], filename_hint="YYYY-MM-DD <Meal>.md"),
    "bodyweight": Schema("bodyweight", "04 - Gym/Body Weight", [
        Field("date", True, kind="date"),
        Field("weight-kg", True, kind="float"),
    ], filename_hint="YYYY-MM-DD Weigh-in.md"),
    "ledger": Schema("ledger", "13 - Finance Tracker", [
        Field("month", True, note='"YYYY-MM", Gregorian'),
        Field("opening-balance-toman", kind="int"),
        Field("closing-balance-toman", kind="int"),
    ], filename_hint="YYYY-MM.md"),
    "category-snapshot": Schema(
        "category-snapshot", "13 - Finance Tracker/Category Snapshots", [
        Field("jalali-month", True, note='"1405-01" — the app categorizes '
              "spending by Iranian calendar month, not Gregorian"),
        Field("gregorian-range", True),
        Field("source", True),
        Field("complete", kind="bool", note="false for the in-progress "
              "current month — its totals will still grow"),
        Field("dining-toman", kind="int"), Field("entertainment-toman", kind="int"),
        Field("transport-toman", kind="int"), Field("groceries-toman", kind="int"),
        Field("health-beauty-toman", kind="int"), Field("culture-arts-toman", kind="int"),
        Field("fashion-toman", kind="int"), Field("bills-toman", kind="int"),
        Field("other-toman", kind="int"), Field("home-toman", kind="int"),
        Field("fitness-toman", kind="int"), Field("savings-toman", kind="int"),
        Field("transfer-toman", kind="int"), Field("shared-expense-toman", kind="int"),
        Field("loan-toman", kind="int"),
    ], filename_hint="YYYY-MM.md (Jalali month)"),
    "finance-profile": Schema("finance-profile", "13 - Finance Tracker", [
        Field("currency", True, note='always "toman" — Rial/10, matches how '
              "Parsa actually talks about money"),
        Field("starting-balance-toman", kind="int",
              note="the balance when tracking began, a fixed historical "
              "anchor like weight-start-kg — current balance is read from "
              "the latest ledger note's balance:: field instead"),
        Field("monthly-income-toman", kind="int"),
        Field("savings-goal-toman", kind="int"),
        Field("savings-goal-target-date", kind="date"),
        Field("dining-budget-toman", kind="int"),
        Field("entertainment-budget-toman", kind="int"),
        Field("transport-budget-toman", kind="int"),
        Field("groceries-budget-toman", kind="int"),
        Field("health-beauty-budget-toman", kind="int"),
        Field("culture-arts-budget-toman", kind="int"),
        Field("fashion-budget-toman", kind="int"),
        Field("bills-budget-toman", kind="int"),
        Field("other-budget-toman", kind="int"),
        Field("home-budget-toman", kind="int"),
        Field("fitness-budget-toman", kind="int"),
        Field("education-budget-toman", kind="int"),
        Field("gold-holdings-grams", kind="float",
              note="manually maintained — the bank statement only records "
              "the toman value of a gold purchase, never the gram weight, "
              "so this can't be derived from the ledger. Update by hand, or "
              "add an optional [grams:: N] inline field to a savings-line "
              "gold purchase going forward and re-sum."),
    ], filename_hint="Finance Profile.md"),
    "lesson": Schema("lesson", None, [
        Field("title", True), Field("date", True, kind="date"),
        Field("category"), Field("status"), Field("project"),
    ]),
    "brag": Schema("brag", None, [
        Field("title", True), Field("date", True, kind="date"),
        Field("category"), Field("project"), Field("impact"),
    ]),
}

# Daily/weekly/monthly notes carry no `type`, so they're matched by path.
DAILY_FIELDS = [Field("date", True, kind="date"), Field("week", True),
                Field("month", True), Field("quarter")]
WEEKLY_FIELDS = [Field("week", True), Field("week-start", True, kind="date")]
MONTHLY_FIELDS = [Field("month", True), Field("month-start", True, kind="date")]

TEMPLATE_TOKEN = re.compile(r"\{\{.*?\}\}")


@dataclass
class Issue:
    rel: str
    level: str          # error | warn
    field: str
    message: str

    def __str__(self) -> str:
        mark = "✗" if self.level == "error" else "!"
        return f"{mark} {self.rel} · {self.field}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    checked: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    def by_file(self) -> dict[str, list[Issue]]:
        out: dict[str, list[Issue]] = {}
        for i in self.issues:
            out.setdefault(i.rel, []).append(i)
        return out

    def summary(self) -> str:
        return (f"{self.checked} notes checked · {len(self.errors)} errors · "
                f"{len(self.warnings)} warnings · "
                f"{len(self.by_file())} files affected")


def schema_for(note: notes.Note) -> tuple[str, list[Field]] | None:
    if note.type and note.type in SCHEMAS:
        s = SCHEMAS[note.type]
        return s.type, s.fields
    rel = note.rel
    if rel.startswith("01 - Daily Notes/Weekly/"):
        return "weekly-note", WEEKLY_FIELDS
    if rel.startswith("01 - Daily Notes/Monthly/"):
        return "monthly-note", MONTHLY_FIELDS
    if rel.startswith("01 - Daily Notes/") and DATE_RE.match(note.path.stem):
        return "daily-note", DAILY_FIELDS
    return None


def _kind_ok(value: Any, kind: str) -> bool:
    if kind == "date":
        return notes.as_date(value) is not None
    if kind == "int":
        try:
            int(str(value).strip().strip('"'))
            return True
        except (TypeError, ValueError):
            return False
    if kind == "float":
        try:
            float(str(value).strip().strip('"'))
            return True
        except (TypeError, ValueError):
            return False
    if kind == "bool":
        return isinstance(value, bool) or str(value).lower() in ("true", "false", "yes", "no")
    if kind == "list":
        return isinstance(value, list)
    return True


def validate_note(note: notes.Note, *, settings: Settings | None = None) -> list[Issue]:
    s = settings or default_settings
    out: list[Issue] = []

    if note.meta.get("_frontmatter_error"):
        return [Issue(note.rel, "error", "frontmatter", "YAML failed to parse")]

    # Unrendered template tokens are the #1 source of broken dashboard rows.
    for k, v in note.meta.items():
        if isinstance(v, str) and TEMPLATE_TOKEN.search(v):
            out.append(Issue(note.rel, "error", k,
                             f"unrendered template token: {v!r}"))

    matched = schema_for(note)
    if not matched:
        return out
    type_name, fields = matched
    sc = SCHEMAS.get(type_name)

    for f in fields:
        present = f.name in note.meta
        value = note.meta.get(f.name)
        empty = value in (None, "", [])

        if f.required and (not present or empty):
            out.append(Issue(note.rel, "error", f.name,
                             f"required by the {type_name} schema but "
                             + ("missing" if not present else "empty")))
            continue
        if empty:
            continue
        if f.enum:
            raw = str(value).strip().strip('"')
            ok_value = raw in f.enum
            # `status` gets the same alias table the queries use, so GitLab's
            # `opened` is not reported as an error. It isn't one: V answers on
            # the normalised value, so the note is *not* invisible to V. It is
            # still a warning, because Dataview compares raw strings and the
            # dashboards genuinely do miss it — but 18 daily "errors" that V
            # itself handles correctly is a nag that teaches you to ignore the
            # validator, which is worse than the mismatch.
            aliased = False
            if not ok_value and f.name == "status":
                from .query import normalise_status
                aliased = normalise_status(raw) in f.enum
            if not ok_value and aliased:
                out.append(Issue(
                    note.rel, "warn", f.name,
                    f"{value!r} means {normalise_status(raw)!r} — V handles "
                    "this, but Dataview compares raw strings, so the note is "
                    "missing from the dashboards"))
            elif not ok_value:
                out.append(Issue(
                    note.rel, "error", f.name,
                    f"{value!r} is not one of {'/'.join(f.enum)} — "
                    "this note will not appear in dashboard queries"))
        if f.kind and not _kind_ok(value, f.kind):
            out.append(Issue(note.rel, "warn", f.name,
                             f"expected {f.kind}, got {value!r}"))

    known = {f.name for f in fields} | {
        "type", "tags", "aliases", "cssclasses", "title", "name", "created",
        "updated", "mood", "energy", "year", "status", "quarter",
    }
    for k in note.meta:
        if k not in known and not k.startswith("_"):
            out.append(Issue(note.rel, "warn", k,
                             f"not in the {type_name} schema (typo?)"))

    if sc and sc.folder and not note.rel.startswith(sc.folder):
        out.append(Issue(note.rel, "warn", "location",
                         f"type is {type_name} but the note is not in {sc.folder}/"))

    if type_name == "daily-note":
        stem = note.path.stem
        d = notes.as_date(note.meta.get("date"))
        if d and stem != d.isoformat():
            out.append(Issue(note.rel, "error", "date",
                             f"frontmatter says {d} but the filename says {stem}"))
    return out


def validate_vault(settings: Settings | None = None, *,
                   skip_templates: bool = True) -> Report:
    s = settings or default_settings
    report = Report()
    for path in notes.iter_notes(s.vault):
        rel = str(path.relative_to(s.vault)).replace("\\", "/")
        if skip_templates and rel.startswith(s.folders.templates):
            continue
        try:
            note = notes.parse(path, s.vault)
        except (OSError, UnicodeDecodeError):
            continue
        report.checked += 1
        report.issues.extend(validate_note(note, settings=s))
    return report


def template_for(type_name: str) -> dict:
    """A blank, schema-valid frontmatter dict — used when creating notes."""
    sc = SCHEMAS.get(type_name)
    if not sc:
        return {"type": type_name}
    meta: dict = {"type": type_name}
    for f in sc.fields:
        meta[f.name] = None
    return meta
