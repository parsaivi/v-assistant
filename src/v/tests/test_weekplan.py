"""Tests for the week's shape — `core/weekplan.py`.

Run:  python3 -m v.tests.test_weekplan

Same no-pytest contract as `test_core.py`: every test builds a throwaway vault
in a temp dir, so none of them can touch the real one.

The tests worth reading first are `a_week_note_is_editable_without_touching_
other_weeks` and `free_time_is_derived_not_written` — between them they are the
entire requirement this module exists to meet.
"""

from __future__ import annotations

import datetime as dt
import os

os.environ["BRAIN_NO_MODEL"] = "1"
os.environ["BRAIN_NO_NOTIFY"] = "1"
os.environ["BRAIN_NO_JALALI"] = "1"

import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import config, notes, weekplan
from v.core.weekplan import Block, WeekPlan

PASS, FAIL = [], []

# A Saturday, so the fixtures read the way the vault's weeks do.
SAT = dt.date(2026, 9, 19)
SUN, MON, TUE = SAT + dt.timedelta(1), SAT + dt.timedelta(2), SAT + dt.timedelta(3)


def test(fn):
    def run():
        try:
            fn()
            PASS.append(fn.__name__)
            print(f"  \x1b[32m✓\x1b[0m {fn.__name__}")
        except Exception as exc:
            FAIL.append((fn.__name__, exc, traceback.format_exc()))
            print(f"  \x1b[31m✗\x1b[0m {fn.__name__}: {exc}")
    run.fn = fn
    return run


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def ok(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


SEED = """# Week Schedule

```
- TH 09:00-10:00 · study · an example that must never be stamped anywhere
```

- SA,MO,WE 08:00-16:00 · work · Divar
- SU,TU 10:30-12:00 · class · Compiler Design @204
- SA,MO,WE 19:00-20:30 · gym
- SU 00:30-08:00 · sleep
"""

WEEKLY_TEMPLATE = """<%*
const d = Number(tp.date.now("d"));
const offset = -((d + 1) % 7);
const saturday = tp.date.now("YYYY-MM-DD", offset);
const weekLabel = moment(saturday).format("GGGG-[W]WW");
const heading = moment(saturday).format("MMMM D, YYYY");
-%>
---
week-start: <% saturday %>
week: "<% weekLabel %>"
tags:
  - weekly-note
---

# Week of <% heading %> (Sat → Fri)

## Weekly Focus

## Schedule

## Weekly Review
"""


def build_vault():
    root = Path(tempfile.mkdtemp(prefix="weekplan-"))
    (root / "01 - Daily Notes/Weekly/2026").mkdir(parents=True)
    (root / "01 - Daily Notes/Calendar").mkdir(parents=True)
    (root / "01 - Daily Notes/2026/09 - September").mkdir(parents=True)
    (root / "05 - Templates").mkdir(parents=True)
    (root / "05 - Templates/Week Schedule.md").write_text(SEED, encoding="utf-8")
    (root / "05 - Templates/Weekly Note.md").write_text(WEEKLY_TEMPLATE,
                                                        encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    s = config.Settings(vault=root, state_dir=root / ".state",
                        model_dir=root / ".models")
    s.ensure_dirs()
    return root, s


# --- parsing ----------------------------------------------------------
@test
def a_block_line_round_trips():
    line = "- 2026-09-20 10:30-12:00 · class · Compiler Design @204"
    b = weekplan.parse_block(line)
    ok(b is not None, "the canonical line must parse")
    eq(b.date, SUN)
    eq(b.kind, "class")
    eq(b.label, "Compiler Design")
    eq(b.location, "204")
    eq(b.minutes, 90)
    eq(b.line(), line, "and render back byte-identical")


@test
def a_block_needs_neither_label_nor_end():
    b = weekplan.parse_block("- 2026-09-19 19:00-20:30 · gym")
    eq(b.kind, "gym")
    eq(b.label, "")
    eq(b.line(), "- 2026-09-19 19:00-20:30 · gym")

    # A time with no end is a marker, not a claim on the day: it must not
    # silently swallow an hour of free time it was never given.
    point = weekplan.parse_block("- 2026-09-19 21:00 · personal · call mom")
    eq(point.end, None)
    eq(point.minutes, 0)


@test
def a_calendar_line_is_not_mistaken_for_a_block():
    """Both files use `- DATE TIME · …`, and they live one folder apart.

    The kind is what separates them, and it is required. Without that, every
    event in `Calendar/2026-09.md` would parse as a block whose kind was the
    first word of its summary — inventing kinds like `1on1` and `demo`, which
    nothing downstream knows how to colour or count.
    """
    ok(weekplan.parse_block("- 2026-09-13 12:00-12:30 · روزانه قیمت گذاری")
       is None, "a Persian summary is not a kind")
    ok(weekplan.parse_block("- 2026-09-06 all-day · deadline") is None)


@test
def day_codes_expand_onto_the_saturday_week():
    rule = weekplan.parse_rule("- SA,MO,WE 08:00-16:00 · work · Divar")
    eq(sorted(rule.days), [0, 2, 5], "SA=5 MO=0 WE=2")
    days = [b.date for b in rule.expand(SAT)]
    eq(days, [SAT, MON, SAT + dt.timedelta(4)], "Sat → Fri order, not Mon → Sun")
    eq(rule.line(), "- SA,MO,WE 08:00-16:00 · work · Divar",
       "and the codes render back in week order")


@test
def a_fenced_example_is_documentation_not_schedule():
    """The seed file has to show its own format without that becoming a rule.

    It did not, and the cost was real: the template shipped three example
    lines under "delete these and write your own", the first `v week --ensure`
    stamped all three into a live week note, and the HUD spent a day drawing
    a job and a class Parsa does not have.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    kinds = [r.kind for r in wp.rules()]
    ok("study" not in kinds, f"a fenced example became a rule: {kinds}")
    eq(len(wp.rules()), 4, "and the real rules below it still parse")
    ok(not any(b.kind == "study" for b in wp.seed_blocks(SAT)),
       "nothing fenced may reach a week note")


@test
def a_bad_day_code_drops_its_line_rather_than_guessing():
    ok(weekplan.parse_rule("- SA,XX 08:00-16:00 · work") is None)


@test
def the_week_is_saturday_to_friday():
    for day in (SAT, SUN, MON, SAT + dt.timedelta(6)):
        eq(weekplan.week_start_of(day), SAT, f"{day} belongs to the week of {SAT}")
    eq(weekplan.week_start_of(SAT - dt.timedelta(1)), SAT - dt.timedelta(7),
       "Friday closes the previous week")


@test
def the_week_note_path_matches_the_vault_that_exists():
    """`01 - Daily Notes/Weekly/2026/2026-W25.md` is a real file in the vault.

    The label is the ISO week of the *Saturday*, and the folder is that
    Saturday's calendar year — which are two different years for one week in
    January, and picking the wrong one files the note in a folder no Dataview
    query looks in.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    eq(wp.week_rel(SUN), "01 - Daily Notes/Weekly/2026/2026-W38.md")
    # Jan 2 2027 is a Saturday whose ISO week belongs to 2026.
    jan = dt.date(2027, 1, 2)
    eq(weekplan.week_start_of(jan), jan)
    eq(wp.week_rel(jan), "01 - Daily Notes/Weekly/2027/2026-W53.md",
       "ISO year names the note, calendar year files it")


# --- seeding ----------------------------------------------------------
@test
def a_new_week_is_seeded_with_what_repeats():
    _, s = build_vault()
    wp = WeekPlan(s)
    rel = wp.ensure_week(SUN)
    eq(rel, "01 - Daily Notes/Weekly/2026/2026-W38.md")

    note = notes.parse(s.vault / rel, s.vault)
    eq(note.meta["week-start"], SAT.isoformat())
    ok("## Weekly Review" in note.body,
       "the body must come from the template, not from Python")

    blocks = wp.week(SAT)
    eq(len(blocks), 9, "3 work + 2 class + 3 gym + 1 sleep")
    eq([b.kind for b in wp.on(SUN)], ["sleep", "class"])
    eq(wp.on(SUN)[1].location, "204")


@test
def a_templater_execution_block_renders_to_nothing():
    """`<%* … %>` is JavaScript, not a token.

    The weekly template opens with one, and V's renderer used to reach inside
    it, find the `tp.date.now("d")` in `const d = Number(tp.date.now("d"))`,
    and render the whole block as a bare `6` — leaving that digit sitting above
    a second, unrendered frontmatter block in every weekly note V scaffolded.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    rel = wp.ensure_week(SAT)
    raw = (s.vault / rel).read_text(encoding="utf-8")

    eq(raw.count("---\n"), 2, "exactly one frontmatter block")
    ok("tp.date.now" not in raw and "const " not in raw,
       "no JavaScript survives into the note")
    ok("\n6\n" not in raw, "and no stray digit where the block was")
    ok("# Week of September 19, 2026 (Sat → Fri)" in raw,
       "the bare names the block would have defined are supplied instead")
    eq(notes.parse(s.vault / rel, s.vault).meta["week"], "2026-W38")


@test
def ensure_week_is_a_no_op_once_the_note_exists():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SUN)
    rel = wp.week_rel(SUN)
    before = (s.vault / rel).read_text(encoding="utf-8")
    wp.ensure_week(MON)                       # same week
    eq((s.vault / rel).read_text(encoding="utf-8"), before,
       "Templater usually gets there first; V must not overwrite it")


@test
def a_week_templater_made_can_still_be_seeded():
    """The other half of "both": Obsidian made the note, V fills the section.

    `reseed` is append-only and deduped, so running it on a week already
    seeded adds nothing and running it after a timetable change adds only the
    new lines.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    rel = wp.week_rel(SAT)
    (s.vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (s.vault / rel).write_text(
        '---\nweek-start: "2026-09-19"\n---\n\n# Week\n\n## Schedule\n\n',
        encoding="utf-8")
    eq(wp.reseed(SAT), 9, "every repeating block is missing, so all land")
    eq(wp.reseed(SAT), 0, "and a second run adds nothing")
    eq(len(wp.week(SAT)), 9)


@test
def a_week_note_is_editable_without_touching_other_weeks():
    """The requirement, stated as a test.

    A birthday added to one week, and a class deleted from it, must leave
    every other week exactly as it was — which is the whole reason the plan
    lives per-week instead of in one standing shape note.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    wp.ensure_week(SAT + dt.timedelta(7))

    wp.add(Block(TUE, dt.time(21, 0), dt.time(23, 0), "personal",
                 "Mahdyar's birthday"))
    rel = wp.week_rel(SAT)
    text = (s.vault / rel).read_text(encoding="utf-8")
    (s.vault / rel).write_text(
        text.replace("- 2026-09-20 10:30-12:00 · class · Compiler Design @204\n", ""),
        encoding="utf-8")

    this_week = wp.week(SAT)
    ok(any(b.label == "Mahdyar's birthday" for b in this_week), "one-off landed")
    ok(not any(b.kind == "class" and b.date == SUN for b in this_week),
       "the cancelled class is gone from this week")

    next_week = wp.week(SAT + dt.timedelta(7))
    eq(len(next_week), 9, "next week still has every repeating block")
    ok(not any(b.label == "Mahdyar's birthday" for b in next_week),
       "and none of this week's one-offs")


@test
def adding_the_same_block_twice_is_a_no_op():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    b = Block(TUE, dt.time(21, 0), dt.time(23, 0), "personal", "birthday")
    wp.add(b)
    before = len(wp.week(SAT))
    out = wp.add(b)
    eq(len(wp.week(SAT)), before, "dedup on (date, time, kind, label)")
    ok("already" in out)


# --- changing a block that is already there ---------------------------
@test
def a_cancelled_class_can_be_dropped():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    line = "- 2026-09-20 10:30-12:00 · class · Compiler Design @204"
    before = len(wp.week(SAT))

    out = wp.remove(SUN, line)
    ok("removed" in out)
    eq(len(wp.week(SAT)), before - 1)
    ok(not any(b.kind == "class" and b.date == SUN for b in wp.week(SAT)))
    # …and the section does not grow a blank line where it was.
    text = (s.vault / wp.week_rel(SAT)).read_text(encoding="utf-8")
    ok("\n\n- 2026-09-20" not in text, "no hole left behind")


@test
def dropping_a_block_that_is_not_there_fails_loudly():
    """The failure mode worth protecting against: a stale view.

    A HUD tab left open on a week somebody has since edited must not delete
    whatever line happens to be nearby. `vault.edit` refuses text it cannot
    find exactly once, and this refuses before it even gets there.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    try:
        wp.remove(SUN, "- 2026-09-20 09:00-10:00 · class · Astrophysics")
        raise AssertionError("a phantom block must not be removable")
    except ValueError as exc:
        ok("no such block" in str(exc))


@test
def a_block_moves_in_place_rather_than_to_the_bottom():
    """Remove-and-append would slowly unsort the section.

    The note is read by a person in Obsidian, and a `## Schedule` whose lines
    drift out of order every time something is rescheduled stops being
    readable long before it stops being correct.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    line = "- 2026-09-20 10:30-12:00 · class · Compiler Design @204"
    out = wp.move(SUN, line, start=dt.time(14, 0), end=dt.time(15, 30))
    ok("moved" in out)

    blocks = wp.week(SAT)
    moved = [b for b in blocks if b.kind == "class" and b.date == SUN]
    eq(len(moved), 1)
    eq(moved[0].time_label, "14:00-15:30")
    eq(moved[0].label, "Compiler Design", "everything unnamed is left alone")
    eq(moved[0].location, "204")

    text = (s.vault / wp.week_rel(SAT)).read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.startswith("- 2026-")]
    # Seeded order is (date, start, kind): Sat work, Sat gym, Sun sleep,
    # Sun class — so the class sits fourth and must still sit fourth.
    eq(lines.index("- 2026-09-20 14:00-15:30 · class · Compiler Design @204"), 3,
       "it stays in the position it was written in")


@test
def a_block_can_change_kind_and_label():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    line = "- 2026-09-19 19:00-20:30 · gym"
    wp.move(SAT, line, kind="paper", label="write the intro")
    b = [x for x in wp.on(SAT) if x.start == dt.time(19, 0)][0]
    eq((b.kind, b.label), ("paper", "write the intro"))


@test
def a_block_cannot_be_given_a_kind_that_does_not_exist():
    """The vocabulary is closed because every consumer switches on it."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    try:
        wp.move(SAT, "- 2026-09-19 19:00-20:30 · gym", kind="yoga")
        raise AssertionError("an unknown kind must be refused")
    except ValueError as exc:
        ok("not a kind" in str(exc))


@test
def a_block_cannot_end_before_it_starts():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    try:
        wp.move(SAT, "- 2026-09-19 19:00-20:30 · gym", end=dt.time(18, 0))
        raise AssertionError("a negative-length block must be refused")
    except ValueError as exc:
        ok("ends before it starts" in str(exc))


@test
def moving_a_block_into_another_week_writes_both_notes():
    """One edit cannot span two files, so it is deliberately two writes."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    nxt = SAT + dt.timedelta(7)
    wp.ensure_week(nxt)
    line = "- 2026-09-19 19:00-20:30 · gym"
    wp.move(SAT, line, date=nxt)

    ok(not any(b.line() == line for b in wp.week(SAT)), "gone from this week")
    landed = [b for b in wp.week(nxt) if b.date == nxt and b.kind == "gym"]
    ok(landed, "and present in the next one")


# --- changing what repeats --------------------------------------------
def _seed(s) -> str:
    return (s.vault / weekplan.SEED_REL).read_text(encoding="utf-8")


@test
def day_names_and_codes_both_read_as_days():
    eq(weekplan.parse_days("SA,MO"), (5, 0))
    eq(weekplan.parse_days("monday and saturday"), (5, 0), "week order, not input")
    eq(weekplan.parse_days("sun tue"), (6, 1))
    eq(weekplan.parse_days("MO,WD"), None, "a typo is not silently dropped")
    eq(weekplan.parse_days(""), None)


@test
def a_new_repeating_block_reaches_this_week_from_today_on():
    """Added on a Monday: this Monday and every later day get it, the
    Saturday already lived does not, and the next week is seeded with it."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    rule = weekplan.Rule((5, 0), dt.time(9), dt.time(10, 30), "class", "MLOps")
    out = wp.add_rule(rule, today=MON)

    ok("repeats" in out)
    ok("- SA,MO 09:00-10:30 · class · MLOps" in _seed(s))
    mlops = [b.date for b in wp.week(SAT) if b.label == "MLOps"]
    eq(mlops, [MON], "the Saturday already lived stays as it was lived")
    nxt = SAT + dt.timedelta(7)
    wp.ensure_week(nxt)
    eq([b.date for b in wp.week(nxt) if b.label == "MLOps"],
       [nxt, nxt + dt.timedelta(2)], "and every later week is stamped with it")

    eq(wp.add_rule(rule, today=MON).split(" ·")[0], "already repeats")


@test
def a_repeating_block_needs_a_real_kind_and_a_real_span():
    _, s = build_vault()
    wp = WeekPlan(s)
    for bad, why in (
            (weekplan.Rule((4,), dt.time(22, 30), dt.time(0), "personal"),
             "ends before it starts"),
            (weekplan.Rule((4,), dt.time(22, 30), None, "futsal"),
             "not a kind")):
        try:
            wp.add_rule(bad, today=SAT)
            raise AssertionError(f"{bad} must be refused")
        except ValueError as exc:
            ok(why in str(exc), str(exc))
    eq(_seed(s), SEED, "a refused rule writes nothing")


@test
def stopping_one_day_of_a_repeating_block_keeps_the_others():
    """`SA,MO,WE` work, Monday dropped: the line becomes `SA,WE`, and this
    week's Monday and Wednesday lose only the Monday."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    out = wp.remove_rule(0, dt.time(8), today=SUN)

    ok("no longer repeats on MO" in out, out)
    ok("- SA,WE 08:00-16:00 · work · Divar" in _seed(s))
    ok("SA,MO,WE 08:00" not in _seed(s).split("```")[-1])
    work = [b.date for b in wp.week(SAT) if b.kind == "work"]
    eq(work, [SAT, SAT + dt.timedelta(4)], "Saturday lived, Wednesday kept")


@test
def stopping_a_repeating_block_on_every_day_deletes_its_line():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.remove_rule(6, dt.time(10, 30), all_days=True, today=SAT)
    ok("Compiler Design" not in _seed(s))
    ok("\n\n\n" not in _seed(s), "no hole left behind")
    eq([r.kind for r in wp.rules()], ["work", "gym", "sleep"])


@test
def moving_a_repeating_block_on_all_days_rewrites_this_week_in_place():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    out = wp.move_rule(6, dt.time(10, 30), all_days=True, to_start=dt.time(14),
                       end=dt.time(15, 30), location="305", today=SAT)

    ok("repeats now · SU,TU 14:00-15:30 · class · Compiler Design @305" in out,
       out)
    eq(len([r for r in wp.rules() if r.kind == "class"]), 1, "one line, edited")
    classes = [(b.date, b.time_label, b.location)
               for b in wp.week(SAT) if b.kind == "class"]
    eq(classes, [(SUN, "14:00-15:30", "305"), (TUE, "14:00-15:30", "305")])


@test
def moving_one_day_of_a_repeating_block_splits_the_rule():
    """"Tuesday's Compiler class is at 14:00 now" leaves Sunday's alone."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    wp.move_rule(1, dt.time(10, 30), to_start=dt.time(14), end=dt.time(15, 30),
                 today=SAT)

    ok("- SU 10:30-12:00 · class · Compiler Design @204\n"
       "- TU 14:00-15:30 · class · Compiler Design @204" in _seed(s),
       "the moved part sits directly under what stayed")
    classes = [(b.date, b.time_label) for b in wp.week(SAT) if b.kind == "class"]
    eq(classes, [(SUN, "10:30-12:00"), (TUE, "14:00-15:30")])


@test
def moving_a_repeating_block_to_another_day_moves_it_this_week_too():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    wp.move_rule(5, dt.time(19), days=(3,), today=SAT)   # Saturday gym → Thursday
    gym = [b.date for b in wp.week(SAT) if b.kind == "gym"]
    eq(gym, [MON, SAT + dt.timedelta(4), SAT + dt.timedelta(5)],
       "Saturday's gym is gone and Thursday has it")
    ok("- MO,WE 19:00-20:30 · gym\n- TH 19:00-20:30 · gym" in _seed(s))


@test
def a_week_line_changed_by_hand_is_left_alone():
    """That week's line is that week's decision; the seed change says so."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    wp.move(TUE, "- 2026-09-22 10:30-12:00 · class · Compiler Design @204",
            label="Compiler Design (makeup)")
    out = wp.move_rule(6, dt.time(10, 30), all_days=True, to_start=dt.time(9),
                       end=dt.time(10, 30), today=SAT)

    ok("left alone" in out and TUE.isoformat() in out, out)
    tue = [b for b in wp.on(TUE) if b.kind == "class"][0]
    eq((tue.time_label, tue.label), ("10:30-12:00", "Compiler Design (makeup)"))


@test
def a_repeating_block_that_is_not_there_fails_loudly():
    _, s = build_vault()
    wp = WeekPlan(s)
    try:
        wp.remove_rule(4, dt.time(22, 30), today=SAT)
        raise AssertionError("a phantom rule must not be removable")
    except ValueError as exc:
        ok("nothing repeats at 22:30 on FR" in str(exc), str(exc))
    try:
        wp.remove_rule(3, dt.time(9), today=SAT)
        raise AssertionError("the fenced example is not a rule")
    except ValueError:
        pass
    eq(_seed(s), SEED)


@test
def the_first_repeating_block_creates_the_seed():
    root, s = build_vault()
    (root / weekplan.SEED_REL).unlink()
    wp = WeekPlan(s)
    wp.add_rule(weekplan.Rule((4,), dt.time(22, 30), dt.time(23, 59),
                              "personal", "Futsal"), today=SAT)
    eq([r.label for r in wp.rules()], ["Futsal"])
    ok("type: week-schedule" in _seed(s))


# --- the point of the module ------------------------------------------
@test
def free_time_is_derived_not_written():
    """Nothing anywhere records "free". It is whatever nothing else claimed."""
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)

    # Saturday: work 08:00-16:00, gym 19:00-20:30, day ends 23:00.
    gaps = wp.free(SAT, min_minutes=30)
    eq([(g.start.strftime("%H:%M"), g.end.strftime("%H:%M")) for g in gaps],
       [("16:00", "19:00"), ("20:30", "23:00")])
    eq(sum(g.minutes for g in gaps), 330)

    # …and a slot shorter than the floor is not a slot.
    eq(len(wp.free(SAT, min_minutes=181)), 0, "3h and 2h30 both fall short")


@test
def sleep_bounds_the_day_so_free_time_is_not_offered_at_four_am():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    # Sunday declares sleep 00:30-08:00 and class 10:30-12:00.
    eq(wp.bounds(SUN), (dt.time(8, 0), dt.time(23, 0)))
    gaps = wp.free(SUN, min_minutes=30)
    eq(gaps[0].start, dt.time(8, 0), "the day opens when sleep ends")
    ok(all(g.start >= dt.time(8, 0) for g in gaps))


@test
def a_calendar_event_makes_you_busy():
    """A one-off meeting lives only in `Calendar/YYYY-MM.md`.

    Free time computed from the week note alone would offer an hour that is
    already a meeting — the single most useful thing this module can get
    wrong, so it is the one with a test.
    """
    _, s = build_vault()
    (s.vault / "01 - Daily Notes/Calendar/2026-09.md").write_text(
        '---\ntype: calendar\nmonth: 2026-09\n---\n\n## Events\n'
        "- 2026-09-19 17:00-18:00 · 1:1 with Arash @Room 3\n", encoding="utf-8")
    wp = WeekPlan(s)
    wp.ensure_week(SAT)

    kinds = [(b.kind, b.time_label) for b in wp.busy(SAT)]
    ok(("event", "17:00-18:00") in kinds, "events fold in as blocks")
    gaps = [(g.start.strftime("%H:%M"), g.end.strftime("%H:%M"))
            for g in wp.free(SAT, min_minutes=30)]
    eq(gaps, [("16:00", "17:00"), ("18:00", "19:00"), ("20:30", "23:00")],
       "the meeting carves the afternoon in two")


@test
def an_event_already_written_as_a_block_is_not_counted_twice():
    _, s = build_vault()
    (s.vault / "01 - Daily Notes/Calendar/2026-09.md").write_text(
        '---\ntype: calendar\nmonth: 2026-09\n---\n\n## Events\n'
        "- 2026-09-20 10:30-12:00 · Compiler Design @204\n", encoding="utf-8")
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    matches = [b for b in wp.busy(SUN) if b.time_label == "10:30-12:00"]
    eq(len(matches), 1, "the gcal import and the timetable are one obligation")
    eq(matches[0].kind, "class", "and the block's own kind wins")


@test
def overlapping_blocks_do_not_invent_a_gap_between_them():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    # A meeting inside work hours: still one continuous busy stretch.
    wp.add(Block(SAT, dt.time(10, 0), dt.time(11, 0), "event", "standup"))
    gaps = [(g.start.strftime("%H:%M"), g.end.strftime("%H:%M"))
            for g in wp.free(SAT, min_minutes=30)]
    eq(gaps, [("16:00", "19:00"), ("20:30", "23:00")])


@test
def now_block_says_what_you_are_in_the_middle_of():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    at_work = wp.now_block(dt.datetime.combine(SAT, dt.time(9, 30)))
    eq(at_work.kind, "work")
    eq(wp.now_block(dt.datetime.combine(SAT, dt.time(17, 0))), None,
       "an unclaimed hour is your own")


@test
def next_free_searches_forward_from_now():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    gap = wp.next_free(120, start=dt.datetime.combine(SAT, dt.time(9, 0)))
    eq((gap.date, gap.start.strftime("%H:%M")), (SAT, "16:00"),
       "not the hour you are already in a meeting for")
    tonight = wp.next_free(120, start=dt.datetime.combine(SAT, dt.time(21, 0)))
    eq((tonight.date, tonight.minutes), (SAT, 120),
       "21:00 to 23:00 is exactly two hours, and exactly enough")
    late = wp.next_free(180, start=dt.datetime.combine(SAT, dt.time(21, 0)))
    ok(late.date > SAT, "three hours is not, so it rolls to tomorrow")


@test
def load_counts_the_weeks_hours_by_kind():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    load = wp.load(SAT)
    eq(load["work"], 3 * 8 * 60)
    eq(load["class"], 2 * 90)
    eq(load["gym"], 3 * 90)


# --- rendering back into notes ----------------------------------------
@test
def syncing_a_daily_note_touches_exactly_one_section():
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    rel = f"01 - Daily Notes/2026/09 - September/{SAT.isoformat()}.md"
    (s.vault / rel).write_text(
        '---\ndate: "2026-09-19"\n---\n\n# Saturday\n\n'
        "## ⏱ Schedule\n\n_placeholder_\n\n## Work Log\n\nhand written\n",
        encoding="utf-8")
    body = wp.sync_daily_note(SAT)
    ok(body is not None, "the section is there, so the sync lands")
    text = (s.vault / rel).read_text(encoding="utf-8")
    ok("**work**" in text and "Free (" in text)
    ok("hand written" in text, "every other section is left alone")
    ok("_placeholder_" not in text)

    eq(wp.sync_daily_note(SAT + dt.timedelta(30)), None,
       "a day with no note is a no-op, not an error")


@test
def a_daily_note_written_before_this_feature_gains_the_section():
    """Every daily note in the vault predates `## ⏱ Schedule`.

    Refusing to sync them would make the whole feature invisible until the
    next new note, so the section is added once, after Calendar and before
    Tasks — where the template puts it.
    """
    _, s = build_vault()
    wp = WeekPlan(s)
    wp.ensure_week(SAT)
    rel = f"01 - Daily Notes/2026/09 - September/{SAT.isoformat()}.md"
    (s.vault / rel).write_text(
        '---\ndate: "2026-09-19"\n---\n\n# Saturday\n\n'
        "## 📅 Calendar\n\n_No events scheduled._\n\n"
        "## Tasks\n\n- [ ] something Parsa typed\n",
        encoding="utf-8")
    ok(wp.sync_daily_note(SAT) is not None)
    text = (s.vault / rel).read_text(encoding="utf-8")
    ok(text.index("## 📅 Calendar") < text.index("## ⏱ Schedule")
       < text.index("## Tasks"), "events, then the shape of the day")
    ok("- [ ] something Parsa typed" in text, "his own lines are untouched")
    # …and a second sync replaces the section rather than adding another.
    wp.sync_daily_note(SAT)
    eq((s.vault / rel).read_text(encoding="utf-8").count("## ⏱ Schedule"), 1)


@test
def a_vault_with_no_seed_yet_still_works():
    """The day before a semester starts, nothing repeats. That is not an error."""
    root, s = build_vault()
    (root / "05 - Templates/Week Schedule.md").unlink()
    wp = WeekPlan(s)
    eq(wp.rules(), [])
    rel = wp.ensure_week(SAT)
    ok("Nothing scheduled yet" in (s.vault / rel).read_text(encoding="utf-8"))
    eq(wp.free(SAT), [weekplan.Gap(SAT, dt.time(8, 0), dt.time(23, 0))],
       "an empty day is fifteen free hours, not zero")


@test
def natural_language_lands_a_block():
    b = weekplan.parse_natural("gym tuesday 19:00-20:30", today=SAT)
    eq((b.kind, b.date, b.time_label), ("gym", TUE, "19:00-20:30"))
    b = weekplan.parse_natural("class 2026-09-20 10:30-12:00 Compiler @204",
                               today=SAT)
    eq((b.kind, b.date, b.label, b.location),
       ("class", SUN, "Compiler", "204"))
    eq(weekplan.parse_natural("something with no time at all"), None)


# ---------------------------------------------------------------------
def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
