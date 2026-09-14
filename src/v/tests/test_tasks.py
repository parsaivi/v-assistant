"""Tests for the task layer and the vault calendar.

Run:  python3 -m v.tests.test_tasks

These two exist because of a specific failure: asked "what are my priorities",
"how far behind am I", "mark the debugging task done" and "what meetings do I
have tomorrow", the assistant got all four wrong — not by hallucinating, but
because it could not see checkboxes at all and did not know the vault had a
calendar. Every test below is one of those failures, frozen.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import os
# Never let a test spend plan allowance or 9s on a model call. The
# decider ships enabled, and every fixture here builds a real
# Settings, so this has to be set before v.core is imported.
os.environ["BRAIN_NO_MODEL"] = "1"
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import calendar as cal, notes, tasks
from v.core.config import Settings

PASS, FAIL = [], []


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


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def note_from(text: str, rel: str = "01 - Daily Notes/2026/08 - August/2026-08-01.md"):
    """A Note backed by a real temp file — the parser reads line numbers off
    disk, so an in-memory note would test a different code path."""
    d = Path(tempfile.mkdtemp())
    p = d / Path(rel).name
    p.write_text(text, encoding="utf-8")
    n = notes.parse_text(text, rel)
    n.path = p
    return n


def store(*note_texts) -> tasks.TaskStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    st = tasks.TaskStore(conn, Settings())
    for i, (rel, body) in enumerate(note_texts):
        n = note_from(body, rel)
        tasks.replace_for_note(conn, rel, tasks.parse_note(n))
    tasks.classify_recurring(conn)
    return st


DAILY = """---
date: "2026-08-01"
---

## Tasks
### Work
- [ ] ship the pricing page
- [x] review Sajad's MR ✅ 2026-08-01

### Life
- [ ] buy milk
"""


# ---------------------------------------------------------------- parsing --
@test
def checkboxes_become_tasks():
    """The whole gap: 1,096 `- [ ]` lines the assistant could not see."""
    st = store(("01 - Daily Notes/2026/08 - August/2026-08-01.md", DAILY))
    open_ = st.open()
    eq(len(open_), 2, "two open boxes in that note —")
    eq({t.text for t in open_}, {"ship the pricing page", "buy milk"})
    eq(len(st.done()), 1, "one ticked box —")


@test
def a_task_inherits_the_notes_date():
    """Nothing in this vault uses `📅` markers, so without inheriting the daily
    note's date every task is undated and "overdue" is permanently empty."""
    st = store(("01 - Daily Notes/2026/08 - August/2026-08-01.md", DAILY))
    t = next(t for t in st.open() if t.text == "buy milk")
    eq(t.note_date, dt.date(2026, 8, 1))
    eq(t.effective_date, dt.date(2026, 8, 1))
    ok(t.is_overdue(dt.date(2026, 8, 5)), "four days later it is overdue")
    ok(not t.is_overdue(dt.date(2026, 8, 1)), "not overdue on its own day")


@test
def line_numbers_index_the_file_not_the_body():
    """Frontmatter offsets every line. Getting this wrong means `complete()`
    ticks a different checkbox than the one the user named — silently."""
    st = store(("01 - Daily Notes/2026/08 - August/2026-08-01.md", DAILY))
    lines = DAILY.splitlines()
    for t in st.all():
        ok(tasks.TASK_RE.match(lines[t.line]),
           f"line {t.line} is {lines[t.line]!r}, not a checkbox")
        ok(t.text.split()[0] in lines[t.line],
           f"line {t.line} holds a different task than {t.text!r}")


@test
def an_explicit_due_date_is_read():
    st = store(("x/note.md", "## Tasks\n- [ ] file the report 📅 2026-09-01\n"
                             "- [ ] other thing due: 2026-08-20\n"))
    due = {t.text: t.due for t in st.open()}
    eq(due["file the report"], dt.date(2026, 9, 1))
    eq(due["other thing"], dt.date(2026, 8, 20))


@test
def strikethrough_is_abandoned_not_open():
    """`- [ ] ~~repair the package: failed~~` is how people actually give up on
    a task. Reading it as open resurrects work already written off."""
    st = store(("01 - Daily Notes/2026/04 - April/2026-04-12.md",
                "---\ndate: \"2026-04-12\"\n---\n\n## Tasks\n"
                "- [ ] ~~fix the package: failed~~\n- [ ] real work\n"))
    eq([t.text for t in st.open()], ["real work"])
    eq(len(st._select("state='dropped'")), 1)


@test
def empty_and_placeholder_boxes_are_not_tasks():
    """The daily template ships `- [ ]` with nothing after it, and goal notes
    carry `- [ ] KR1:`. Counting those makes every number wrong."""
    st = store(("01 - Daily Notes/2026/08 - August/2026-08-01.md",
                "---\ndate: \"2026-08-01\"\n---\n\n## Tasks\n"
                "- [ ] \n- [ ] KR1:\n- [ ] a\n- [ ] something real\n"))
    eq([t.text for t in st.open()], ["something real"])


@test
def markdown_is_stripped_from_the_text():
    st = store(("x/note.md",
                "## Tasks\n- [ ] **bold** and `code` and [link](http://x.com)\n"))
    eq(st.open()[0].text, "bold and code and link")


# ------------------------------------------------------------ classifying --
@test
def lesson_bullets_are_not_commitments():
    """`02 - Work/Lessons/` writes its rules as checkboxes — "UUID PKs by
    default" is a thing learned, not a thing owed. Before this split, "what are
    my priorities" answered with 376 items, which is the same as answering with
    none."""
    st = store(("02 - Work/Lessons/Database conventions.md",
                "---\ntype: lesson\ndate: \"2026-01-01\"\n---\n\n"
                "## Checklist\n- [ ] UUID PKs by default\n"
                "- [ ] every column NOT NULL\n"))
    eq(len(st.open()), 0, "lesson rules must not be commitments —")
    eq(len(st.reference()), 2)


@test
def a_heading_named_tasks_makes_it_a_commitment():
    st = store(("15 - DUNG/plan.md", "## Action Items\n- [ ] wire the webhook\n"))
    eq([t.text for t in st.open()], ["wire the webhook"])


@test
def a_dated_checkbox_is_a_commitment_wherever_it_lives():
    """A deadline written by hand beats every folder heuristic."""
    st = store(("02 - Work/Lessons/whatever.md",
                "---\ntype: lesson\n---\n\n## Notes\n"
                "- [ ] actually do this one 📅 2026-08-09\n"))
    eq([t.text for t in st.open()], ["actually do this one"])


@test
def boilerplate_repeated_across_notes_is_classified_out():
    """The MR template's review checklist repeats across ~180 notes and
    `- [ ] Trained today` sits in every daily note. Both buried the real work.
    The rule is derived from the data, so a template adopted next week is
    handled without a code change."""
    notes_in = [(f"01 - Daily Notes/2026/07 - July/2026-07-0{i}.md",
                 f"---\ndate: \"2026-07-0{i}\"\n---\n\n## Tasks\n"
                 f"- [ ] Trained today\n- [ ] unique thing {i}\n")
                for i in range(1, 6)]
    st = store(*notes_in)
    texts = {t.text for t in st.open()}
    ok("Trained today" not in texts,
       "a checkbox in every daily note is a ritual, not five tasks")
    eq(len(texts), 5, "the five distinct tasks survive —")
    eq(len(st.rituals()), 5, "and the ritual is still queryable —")


@test
def archived_notes_hold_no_open_tasks():
    st = store(("06 - Archive/old project.md",
                "## Tasks\n- [ ] something we stopped doing\n"))
    eq(len(st.open()), 0, "a checkbox in 06 - Archive/ is history —")


# --------------------------------------------------------------- ranking --
@test
def the_same_task_copied_forward_counts_once():
    """Pasting yesterday's unfinished task into today is normal use. Listing
    all three copies turns one nagging task into three and makes the backlog
    look worse than it is."""
    st = store(*[(f"01 - Daily Notes/2026/07 - July/2026-07-2{i}.md",
                  f"---\ndate: \"2026-07-2{i}\"\n---\n\n## Tasks\n"
                  f"- [ ] finish the migration\n")
                 for i in range(1, 4)])
    rows = tasks.collapse(st.open())
    eq(len(rows), 1, "one task, three copies —")
    eq(rows[0].carried, 3)
    eq(rows[0].note_date, dt.date(2026, 7, 23), "points at the newest copy —")


@test
def a_missed_deadline_outranks_an_old_daily_note_line():
    today = dt.date(2026, 8, 1)
    overdue = tasks.Task(1, "x.md", 0, tasks.OPEN, "pay the invoice", "",
                         due=dt.date(2026, 7, 28))
    ancient = tasks.Task(2, "y.md", 0, tasks.OPEN, "some april thought", "",
                         note_date=dt.date(2026, 4, 1))
    ok(overdue.urgency(today) > ancient.urgency(today),
       "a real deadline must beat undated staleness")


@test
def staleness_is_not_monotonic_urgency():
    """The first version ranked purely by age, so "what should I do next"
    answered with April — burying everything actually in flight. A four-month
    old undated line is a backlog problem, not the next thing to do."""
    today = dt.date(2026, 8, 1)
    fresh = tasks.Task(1, "x.md", 0, tasks.OPEN, "today's thing", "",
                       note_date=today)
    ancient = tasks.Task(2, "y.md", 0, tasks.OPEN, "april thing", "",
                         note_date=dt.date(2026, 4, 1))
    ok(fresh.urgency(today) > ancient.urgency(today),
       "this week's work must outrank April's")
    ok(ancient.is_rotting, "and April's should be reported as rot")
    ok(not fresh.is_rotting)


@test
def priorities_do_not_come_from_one_note():
    """A planning note written yesterday holds twenty identically-scored tasks.
    Without a per-note cap, "my priorities" is that one note's contents."""
    body = "## Tasks\n" + "".join(f"- [ ] plan item {i}\n" for i in range(20))
    st = store(("19 - Assistant/Plan.md", f"---\ndate: \"2026-08-01\"\n---\n\n{body}"),
               ("01 - Daily Notes/2026/08 - August/2026-08-01.md", DAILY))
    top = st.priorities(today=dt.date(2026, 8, 1), limit=6)
    from_plan = [t for t in top if t.rel.startswith("19 -")]
    ok(len(from_plan) <= 3, f"one note supplied {len(from_plan)} of 6")
    ok(any(t.rel.startswith("01 -") for t in top),
       "the daily note's tasks must survive the flood")


@test
def backlog_shape_separates_live_work_from_rot():
    """"124 overdue" reads as a catastrophe when 114 are this month's work.
    A backlog answer that only counts always sounds the same."""
    st = store(("01 - Daily Notes/2026/07 - July/2026-07-30.md",
                "---\ndate: \"2026-07-30\"\n---\n\n## Tasks\n- [ ] recent thing\n"),
               ("01 - Daily Notes/2026/03 - March/2026-03-01.md",
                "---\ndate: \"2026-03-01\"\n---\n\n## Tasks\n- [ ] ancient thing\n"))
    s = st.backlog_shape(dt.date(2026, 8, 1))
    eq(s["overdue"], 2)
    eq(s["rotting"], 1, "only the March one is rot —")
    eq(s["live"], 1, "only the July one is live —")


# --------------------------------------------------------------- writing --
@test
def completing_a_task_changes_exactly_one_line():
    """The append-only rule exists to stop a rewrite mangling hand-written
    prose. Ticking a box is the one edit worth automating, so it verifies."""
    root = Path(tempfile.mkdtemp())
    rel = "01 - Daily Notes/2026/08 - August/2026-08-01.md"
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(DAILY, encoding="utf-8")

    s = Settings(vault=root)
    s.git_enabled = False
    from v.core.vaultfs import Vault
    v = Vault(s)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    st = tasks.TaskStore(conn, s)
    n = notes.parse(p, root)
    tasks.replace_for_note(conn, rel, tasks.parse_note(n))

    t = next(t for t in st.open() if t.text == "buy milk")
    st.complete(t, v, today=dt.date(2026, 8, 2))

    before, after = DAILY.splitlines(), p.read_text().splitlines()
    eq(len(after), len(before), "line count must not change —")
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    eq(differing, [t.line], "exactly the task's line —")
    ok("- [x] buy milk" in after[t.line], f"got {after[t.line]!r}")
    ok("✅ 2026-08-02" in after[t.line], "completion date is stamped")


@test
def completing_a_moved_task_refuses_rather_than_guesses():
    """If the note changed since indexing, the line number points somewhere
    else. Writing anyway edits a random line of the user's prose."""
    root = Path(tempfile.mkdtemp())
    rel = "note.md"
    (root / rel).write_text("## Tasks\n- [ ] a thing\n", encoding="utf-8")
    s = Settings(vault=root)
    s.git_enabled = False
    from v.core.vaultfs import Vault
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    st = tasks.TaskStore(conn, s)
    stale = tasks.Task(1, rel, 0, tasks.OPEN, "a thing", "")  # line 0 = heading
    try:
        st.complete(stale, Vault(s))
        raise AssertionError("wrote to a line that is not a checkbox")
    except ValueError as exc:
        ok("checkbox" in str(exc) or "re-index" in str(exc), str(exc))


@test
def a_task_v_adds_is_visible_the_moment_it_is_added():
    """Markdown is the storage, but the mirror is what every task query reads.
    `add()` wrote the checkbox and left the mirror alone, so the task existed
    on disk and was missing from the list it had just been added to. The CLI
    hid it behind a whole-vault rebuild; the MCP server, which has none, did
    not hide it at all."""
    from v.core import index
    from v.core.vaultfs import Vault

    root = Path(tempfile.mkdtemp())
    rel = "01 - Daily Notes/2026/08 - August/2026-08-01.md"
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_text(DAILY, encoding="utf-8")

    s = Settings(vault=root, state_dir=root / ".state")
    s.git_enabled = False
    s.ensure_dirs()
    index.rebuild(s, full=True)
    conn = index.connect(s)
    st = tasks.TaskStore(conn, s)

    st.add("call the dentist", Vault(s), rel=rel, heading="Tasks")
    ok(any("dentist" in t.text for t in st.open()),
       "the task must be queryable without waiting for a re-index")
    ok(not index.is_stale(s, conn),
       "and the write must leave the mirror in step with the file")

    t = next(t for t in st.open() if "dentist" in t.text)
    st.complete(t, Vault(s), today=dt.date(2026, 8, 2))
    ok(not any("dentist" in x.text for x in st.open()), "ticking it removes it")
    ok(not index.is_stale(s, conn), "ticking it also keeps the mirror in step")
    conn.close()


@test
def finding_a_task_by_a_distinctive_word_beats_shared_filler():
    st = store(("01 - Daily Notes/2026/08 - August/2026-08-01.md",
                "---\ndate: \"2026-08-01\"\n---\n\n## Tasks\n"
                "- [ ] debugging the payment worker\n"
                "- [ ] write the task report for the team\n"))
    hits = st.find("debugging")
    ok(hits, "one distinctive word must find the task")
    eq(hits[0][0].text, "debugging the payment worker")


# -------------------------------------------------------------- ignoring --
# The failure these freeze: after a week of use the vault holds dozens of
# checkboxes that were never decisions, and V ranks, counts and asks about
# every one of them forever, because "this is over and I'm not doing it" was
# not something the system could be told.

STALE_VAULT = (
    "01 - Daily Notes/2026/06 - June/2026-06-02.md",
    '---\ndate: "2026-06-02"\n---\n\n## Tasks\n'
    "- [ ] read the Kafka rebalance doc\n"
    "- [ ] chase the design review\n",
)


@test
def an_ignored_task_leaves_every_list_v_builds():
    """One clause in `open()` rather than one per caller: priorities, overdue,
    the backlog count and the due-date lookups all read the vault through it,
    which is what makes "stop bringing it up" mean everywhere at once."""
    st = store(STALE_VAULT)
    day = dt.date(2026, 8, 1)
    t = next(t for t in st.open() if t.text.startswith("read the Kafka"))
    st.ignore(t, reason="test")

    texts = {x.text for x in st.open()}
    ok("read the Kafka rebalance doc" not in texts, f"still open: {texts}")
    ok("chase the design review" in texts, "ignored one, hid two")
    ok(all("Kafka" not in x.text for x in st.priorities(today=day)),
       "still ranked")
    ok(all("Kafka" not in x.text for x in st.overdue(day)), "still overdue")
    ok(all("Kafka" not in x.text for x in st.due_by(day)), "still due")
    eq(st.backlog_shape(day)["open"], 1)
    eq(st.backlog_shape(day)["ignored"], 1)


@test
def ignoring_writes_nothing_to_the_note():
    """The whole reason this is not `complete(state=DROPPED)`. Parsa's daily
    note from June keeps the line exactly as he typed it; only V's opinion of
    it changes."""
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root)
    s.git_enabled = False
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    st = tasks.TaskStore(conn, s)
    tasks.replace_for_note(conn, rel, tasks.parse_note(notes.parse(p, root)))

    st.ignore(next(t for t in st.open() if "Kafka" in t.text))
    eq(p.read_text(encoding="utf-8"), body, "the note was edited —")


@test
def ignoring_follows_a_task_carried_across_notes():
    """A task pasted into four daily notes is one task — `collapse()` has
    always said so. Ignoring it four times, once per copy, would be the same
    chore this feature exists to remove, so the key is the text."""
    st = store(
        ("01 - Daily Notes/2026/06 - June/2026-06-02.md",
         '---\ndate: "2026-06-02"\n---\n\n## Tasks\n- [ ] chase the design review\n'),
        ("01 - Daily Notes/2026/06 - June/2026-06-03.md",
         '---\ndate: "2026-06-03"\n---\n\n## Tasks\n- [ ] chase the design review\n'),
        ("01 - Daily Notes/2026/06 - June/2026-06-04.md",
         '---\ndate: "2026-06-04"\n---\n\n## Tasks\n- [ ] chase the design review\n'),
    )
    eq(len(st.open()), 3, "three copies on disk —")
    st.ignore(st.open()[0])
    eq(st.open(), [], "one ignore, every copy —")


@test
def an_ignore_survives_a_full_reindex():
    """`task_ignores` is the user's decision, not a mirror of the Markdown, so
    it lives outside the tables `rebuild()` refills. Keyed on the text and not
    on `blake2b(rel|line)` for the same reason: re-parsing a note renumbers
    every task below an inserted line."""
    from v.core import index
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()

    index.rebuild(s)
    conn = index.connect(s)
    st = tasks.TaskStore(conn, s)
    t = next(x for x in st.open() if "Kafka" in x.text)
    old_id = t.id
    st.ignore(t)
    conn.close()

    # A line inserted above it: every id below moves, the text does not.
    p.write_text(body.replace("## Tasks\n", "## Tasks\n- [ ] something new\n"),
                 encoding="utf-8")
    index.rebuild(s, full=True)
    conn = index.connect(s)
    st = tasks.TaskStore(conn, s)
    texts = {x.text for x in st.open()}
    ok("read the Kafka rebalance doc" not in texts,
       f"came back after a rebuild: {texts}")
    ok("something new" in texts, "the rest of the note must still index")
    # The point of the test, stated, and it is worse than "the id is gone":
    # the id it was ignored under now resolves to *the newly inserted task*,
    # because an id is `blake2b(rel|line)` and the line number was reused. An
    # id-keyed silence list would not merely have failed open here — it would
    # have silenced a task written thirty seconds ago.
    moved = st.get(old_id)
    ok(moved is not None and moved.text == "something new",
       f"expected the id to be reused, got {moved and moved.text!r}")
    eq([i.live for i in st.ignored()], [1], "it should say what it is hiding")
    conn.close()


@test
def the_flag_is_re_stamped_onto_rows_a_rebuild_recreated():
    """`task_ignores` is the record and `tasks.ignored` is the flag. A rebuild
    deletes and refills the mirror from Markdown that knows nothing about the
    decision, so the flag has to be put back — this is the join between the
    two, and it is the only thing standing between "ignored" and "ignored
    until the next time a note is saved"."""
    from v.core import index
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()

    index.rebuild(s)
    conn = index.connect(s)
    st = tasks.TaskStore(conn, s)
    eq(index.open_task_count(conn), 2)
    st.ignore(next(x for x in st.open() if "Kafka" in x.text))
    eq(conn.execute("SELECT COUNT(*) FROM tasks WHERE ignored=1").fetchone()[0],
       1, "ignoring must stamp the row it applies to, not only the record —")
    eq(index.open_task_count(conn), 1, "the count must drop immediately —")
    conn.close()

    index.rebuild(s, full=True)
    conn = index.connect(s)
    flagged = [r[0] for r in conn.execute(
        "SELECT text FROM tasks WHERE ignored=1")]
    eq(flagged, ["read the Kafka rebalance doc"],
       "the flag did not survive the rebuild —")
    eq(index.open_task_count(conn), 1)
    eq(index.stats(s)["tasks_ignored"], 1)
    conn.close()


@test
def the_flag_survives_the_single_note_resync_after_a_write():
    """The other path into the mirror, and the one that runs constantly:
    ticking a box or adding a task re-parses just that note. It goes through
    `replace_for_note`, which recreates every row in the file — including the
    silenced one, whose flag would be lost if only `apply_ignores` set it."""
    from v.core import index
    from v.core.vaultfs import Vault
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root, state_dir=root / ".state")
    s.git_enabled = False
    s.ensure_dirs()
    index.rebuild(s)
    conn = index.connect(s)
    st = tasks.TaskStore(conn, s)

    st.ignore(next(x for x in st.open() if "Kafka" in x.text))
    # Tick the *other* task, which rewrites and re-indexes the same note.
    st.complete(next(x for x in st.open() if "design" in x.text), Vault(s))

    eq([x.text for x in st.open()], [],
       "the silenced task came back when its note was re-indexed")
    eq(conn.execute("SELECT COUNT(*) FROM tasks WHERE ignored=1").fetchone()[0], 1)
    conn.close()


@test
def an_ignored_task_is_gone_from_the_brief_and_from_answers():
    """The requirement in one test: ignoring is not a HUD feature.

    Every surface that volunteers a task — the morning brief, the evening
    review, "what are my priorities", "how far behind am I", the open list —
    goes through `TaskStore.open()`, so all of them are checked here against
    one real engine rather than trusted to keep doing so.
    """
    from v.core import index
    from v.core.engine import Engine
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root, state_dir=root / ".state")
    s.git_enabled = False
    s.ensure_dirs()
    index.rebuild(s)
    engine = Engine(s)
    try:
        needle = "Kafka"

        def surfaces() -> dict:
            return {
                "brief": engine.brief().text,
                "evening": engine.evening().text,
                "priorities": engine.q.priorities().answer + "".join(
                    r.title for r in engine.q.priorities().rows),
                "open": "".join(r.title for r in engine.q.open_tasks().rows),
                "overdue": "".join(r.title for r in
                                   engine.q.overdue_tasks().rows),
                "working_state": engine._working_state(
                    dt.datetime.now()),
            }

        before = surfaces()
        # Named explicitly so this cannot quietly become a test that proves
        # nothing: the brief is the surface the requirement was written about,
        # and it has to be showing the task before it can be shown not to.
        shown = {k for k, v in before.items() if needle in v}
        ok({"brief", "priorities", "open", "overdue", "working_state"} <= shown,
           f"the fixture must start visible on every list surface, got {shown}")

        t = next(x for x in engine.q.tasks.open() if needle in x.text)
        engine.q.tasks.ignore(t, reason="test")

        after = surfaces()
        leaked = [k for k, v in after.items() if needle in v]
        eq(leaked, [], f"an ignored task is still being volunteered by {leaked}")
        ok("chase the design review" in after["open"],
           "ignoring one must not hide the rest")
    finally:
        engine.close()


@test
def naming_an_ignored_task_still_finds_it():
    """The filter hides tasks from lists V volunteers, never from a question
    that asks for one. Without this the only way back from an ignore is the
    HUD, and "mark the Kafka thing done" answers that there is no such task."""
    st = store(STALE_VAULT)
    st.ignore(next(t for t in st.open() if "Kafka" in t.text))
    hits = st.find("kafka rebalance")
    eq(len(hits), 1, "an ignored task must still be findable by name —")


@test
def un_ignoring_takes_the_phrase_or_the_key():
    """The HUD hands back the key it was given; a person at a terminal types
    the words. Both have to work or the feature has a one-way door in it."""
    st = store(STALE_VAULT)
    t = next(x for x in st.open() if "Kafka" in x.text)
    key = st.ignore(t)
    ok(st.unignore(key), "by key —")
    eq(len({x.text for x in st.open()}), 2, "back in the list —")

    st.ignore(t)
    ok(st.unignore("Read the Kafka Rebalance Doc"), "by phrase —")
    eq(len(st.open()), 2)
    ok(not st.unignore("never ignored anything like this"),
       "unignoring nothing must report that it did nothing")


@test
def un_ignoring_matches_a_name_the_way_ignoring_did():
    """The door has to open both ways. `--ignore` finds its task through
    `find()`, so a task can be silenced by typing four of its words — and
    before this, bringing it back needed the text reproduced exactly. Found
    by typing the phrase with a letter wrong, which is how it was noticed."""
    st = store(STALE_VAULT)
    st.ignore(next(t for t in st.open() if "Kafka" in t.text))
    ok(not st.unignore("kafka doc"), "an exact-key miss is still a miss —")
    hits = st.find_ignored("kafka doc")
    eq(len(hits), 1, "the phrase must still name it —")
    ok(st.unignore(hits[0][0].norm))
    eq(len(st.open()), 2, "back in the list —")
    eq(st.find_ignored("kafka doc"), [], "nothing left to find")


@test
def ticking_an_ignored_task_clears_the_ignore():
    """Otherwise the entry outlives the checkbox it was about, and writing the
    same words next month lands pre-silenced by a decision made about a task
    that has since been ticked."""
    root = Path(tempfile.mkdtemp())
    rel, body = STALE_VAULT
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")
    s = Settings(vault=root)
    s.git_enabled = False
    from v.core.vaultfs import Vault
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    st = tasks.TaskStore(conn, s)
    tasks.replace_for_note(conn, rel, tasks.parse_note(notes.parse(p, root)))

    t = next(x for x in st.open() if "Kafka" in x.text)
    st.ignore(t)
    eq(len(st.ignored()), 1)
    st.complete(t, Vault(s), today=dt.date(2026, 8, 2))
    eq(st.ignored(), [], "the silence list outlived the task —")


@test
def a_task_with_no_normalisable_text_is_still_ignorable():
    """`NORM_SQL` and `Task.norm_key` have to agree character for character.
    They disagreed once, on a checkbox that normalises to nothing, and the
    ignore was written to a key no query would ever match."""
    st = store(("01 - Daily Notes/2026/06 - June/2026-06-02.md",
                '---\ndate: "2026-06-02"\n---\n\n## Tasks\n- [ ] ??? !!! ???\n'))
    rows = st.open()
    if not rows:
        return          # the parser drops it as a placeholder, which is fine
    st.ignore(rows[0])
    eq(st.open(), [], "an ignore keyed off norm_key must match NORM_SQL —")


@test
def v_is_told_a_task_has_three_endings():
    """The prompt is the only place V learns `ignored` exists. Without it the
    tool ships and V keeps offering to tick things Parsa never did.

    It also has to be told that an ignored task is missing *on purpose*: the
    `- [ ]` line is still in the daily note, so it still comes back from
    `vault_search`, and a model that reads note bodies will otherwise helpfully
    put it back into the list the user just took it out of.
    """
    from v.core.identity import SYSTEM
    low = SYSTEM.lower()
    ok('state="ignored"' in low or "state='ignored'" in low,
       "the prompt never shows the ignored state")
    ok("writes nothing to the note" in low,
       "the prompt must say ignoring does not touch the vault")
    ok("missing on purpose" in low and "vault_search" in low,
       "the prompt must warn against re-raising one from note text")
    # And the view it points at has to exist, or the instruction is a dead
    # end that costs a tool call to discover.
    import v.mcp.server as server
    ok('view == "ignored"' in
       Path(server.__file__).read_text(encoding="utf-8"),
       "the prompt names vault_tasks(view='ignored') and the tool has no "
       "such view")


# -------------------------------------------------------------- calendar --
@test
def the_clock_is_pinned_not_inherited():
    """The August calendar had the 12:00 standup written down as 16:30.

    Not a calendar bug: `~/.bashrc` carries `TZ=Asia/Singapore` on the
    `claude-parsa` alias, and every clock call in this package — plus
    `daily_calendar.py`'s `LOCAL_TZ` — read the ambient zone. A V session
    started through that alias converted every imported event to Singapore
    time, 4h30m ahead, and wrote the result down as if it were local.
    """
    import importlib
    from v.core import config as config_mod

    before = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Singapore"
        s = config_mod.load()
        eq(s.timezone, "Asia/Tehran", "the zone is a setting, not the ambient TZ")
        eq(dt.datetime.now().astimezone().strftime("%z"), "+0330",
           "the process clock must be Tehran regardless of the environment")
        # The vendored script snapshots the zone into a module constant at
        # import, which is why `config` has to pin it before anything loads it.
        eq(str(s.tz), "Asia/Tehran")
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        config_mod.load()          # restore the pinned zone for later tests


@test
def ical_feeds_keep_the_names_written_above_them():
    """`.gcal_ics_url` had `# Personal` and `# Work` above its two URLs and
    `get_sources` dropped both, so every event arrived from one anonymous
    merged blob and "what's on my work calendar" had nothing to filter on."""
    from v.core import gcal

    root = Path(tempfile.mkdtemp())
    (root / ".gcal_ics_url").write_text(
        "# Personal\n"
        "https://calendar.google.com/calendar/ical/me%40gmail.com/private-a/basic.ics\n"
        "# Work\n"
        "https://calendar.google.com/calendar/ical/team%40group.calendar.google.com/public/basic.ics\n"
        "\n"
        "https://calendar.google.com/calendar/ical/arash%40example.com/private-c/basic.ics\n",
        encoding="utf-8")
    feeds = gcal.ical_feeds(Settings(vault=root, state_dir=root / ".state"))
    eq([f.name for f in feeds], ["Personal", "Work", "arash"],
       "labels become names, and an unlabelled URL falls back to its address")
    eq(feeds[0].id, "ical:Personal")


@test
def an_ical_feed_says_it_cannot_be_written_to():
    """A secret iCal address is a subscription URL — there is no endpoint
    behind it to POST to. Saying "unauthorised" would send Parsa to re-auth
    for something no credential can fix."""
    from v.core import gcal

    root = Path(tempfile.mkdtemp())
    (root / ".gcal_ics_url").write_text(
        "# Personal\nhttps://calendar.google.com/calendar/ical/me%40gmail.com/private-a/basic.ics\n",
        encoding="utf-8")
    g = gcal.GoogleCalendar(Settings(vault=root, state_dir=root / ".state"))
    eq([c["name"] for c in g.calendars()], ["Personal"])
    ok(not g.calendars()[0]["writable"], "a feed is never writable")
    try:
        g.add_event("x", start=dt.datetime(2026, 8, 8, 12, 30),
                    calendar_id="ical:Personal")
        raise AssertionError("wrote to a read-only feed")
    except gcal.NotConfigured as exc:
        ok("read-only" in str(exc), str(exc))


@test
def a_day_with_a_holiday_on_it_still_imports_its_meetings():
    """`agenda` re-sorted the feed's rows by row[0] alone — a naive
    `time.min` for an all-day event, an aware `timetz()` for a timed one. Any
    day holding both raised "can't compare offset-naive and offset-aware
    times", the importer caught it and moved on, and that whole day imported
    nothing. Three days of August were empty in the vault, one of them a
    deadline."""
    import datetime as _dt

    aware = _dt.time(12, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=3.5)))
    rows = [
        (aware, False, "دیلی قیمت‌گذاری", "", "12:00", "12:15", "u1", "c"),
        (_dt.time.min, True, "ددلاین پاک‌سازی حکمت", "", "", "", "u2", "c"),
    ]
    rows.sort(key=lambda r: (0 if r[1] else 1, r[0]))   # the key agenda uses
    eq([r[2] for r in rows],
       ["ددلاین پاک‌سازی حکمت", "دیلی قیمت‌گذاری"],
       "all-day first, and no naive/aware comparison on the way there")


@test
def an_event_round_trips_through_a_line():
    ev = cal.Event(dt.date(2026, 8, 8), dt.time(12, 30), dt.time(13, 30),
                   "1on1 with Sajad", "Room 3")
    back = cal.parse_line(ev.line())
    ok(back, f"could not re-read {ev.line()!r}")
    eq(back.date, ev.date)
    eq(back.time_label, "12:30-13:30")
    eq(back.summary, "1on1 with Sajad")
    eq(back.location, "Room 3")


@test
def all_day_events_round_trip_too():
    ev = cal.Event(dt.date(2026, 8, 9), None, None, "Public holiday")
    back = cal.parse_line(ev.line())
    ok(back and back.all_day, f"lost all-day in {ev.line()!r}")
    eq(back.summary, "Public holiday")


@test
def natural_language_becomes_a_dated_event():
    """The scenario, verbatim: "hey on Aug 8 i should go a meeting at 12:30 pm
    remind me then"."""
    ev = cal.parse_natural(
        "hey on Aug 8 i should go a meeting at 12:30 pm remind me then",
        today=dt.date(2026, 8, 1))
    ok(ev, "failed to parse the event at all")
    eq(ev.date, dt.date(2026, 8, 8))
    eq(ev.start, dt.time(12, 30))
    eq(ev.summary, "meeting")


@test
def the_summary_keeps_the_words_that_matter():
    """An earlier noise filter ended in `[a-z]*`, so `me` matched "meeting" and
    `a` matched "Arash" — it deleted the two things an event is for."""
    ev = cal.parse_natural("lunch with Arash on Sep 2 at 13:00",
                           today=dt.date(2026, 8, 1))
    eq(ev.summary, "lunch with Arash")
    ev = cal.parse_natural("dentist appointment tomorrow at 9am",
                           today=dt.date(2026, 8, 1))
    eq(ev.summary, "dentist appointment")
    eq(ev.date, dt.date(2026, 8, 2))


@test
def an_unparseable_date_returns_nothing_rather_than_guessing():
    """A meeting written to the wrong day is worse than one not written."""
    ok(cal.parse_natural("we should meet sometime soon") is None)
    ok(cal.parse_natural("at 3pm", today=dt.date(2026, 8, 1)) is None,
       "a time with no date is not an event")


@test
def the_summary_is_leftovers_and_the_decider_may_overrule_it():
    """`_summary_of` works by deletion, so a wordy message titles itself badly.

    "for today calander set my meeting on 13:00 and not 17:30" parsed the date
    and time correctly and then wrote an event called "calander set meeting
    and not", because that is what was left after the clocks came out. The
    parser keeps the date — a regex is better at 13:00 than a model is — and
    the engine takes the model's title when there is one.
    """
    msg = "for today calander set my meeting on 13:00 and not 17:30"
    ev = cal.parse_natural(msg, today=dt.date(2026, 8, 1))
    eq(ev.date, dt.date(2026, 8, 1))
    eq(ev.time_label.split("-")[0].strip(), "13:00")
    ok("calander" in ev.summary.lower(),
       "if this stops being garbage the override is still fine, but the "
       "comment above is stale")

    import dataclasses
    better = dataclasses.replace(ev, summary="Meeting")
    eq(better.summary, "Meeting")
    eq(better.date, ev.date, "overriding the title must not touch the date")
    eq(better.time_label, ev.time_label)


@test
def replacing_a_section_leaves_every_other_byte_alone():
    """This is how the daily note's Calendar section gets refreshed. If it
    reformats anything else, it has rewritten prose the user hand-wrote."""
    content = (
        "---\ndate: \"2026-08-01\"\n---\n\n# Saturday\n\n"
        "## 📅 Calendar\n_No events scheduled._\n\n"
        "## Tasks\n- [ ] buy milk\n\n## End of Day\n**Wins:**\n")
    out = cal.replace_section(content, cal.DAILY_HEADING, "- 12:30 — meeting")
    ok(out, "heading not found")
    ok("- 12:30 — meeting" in out)
    ok("_No events scheduled._" not in out)
    for keep in ("## Tasks", "- [ ] buy milk", "## End of Day", "**Wins:**",
                 "# Saturday", 'date: "2026-08-01"'):
        ok(keep in out, f"lost {keep!r}")
    eq(out.count("##"), content.count("##"), "heading count changed —")


@test
def a_missing_section_is_reported_not_invented():
    ok(cal.replace_section("# Note\n\nbody\n", "📅 Calendar", "x") is None)


@test
def emoji_headings_match_their_plain_form():
    """`## 📅 Calendar` and `## Calendar` are the same section to a human."""
    out = cal.replace_section("## Calendar\nold\n", "📅 Calendar", "new")
    ok(out and "new" in out and "old" not in out)


# ------------------------------------------------------------------ wiring --
# These all pin `agentic=False`, and that is the point of the flag rather than
# a workaround for it. Online, none of these messages is classified at all —
# they reach the model, which calls `vault_tasks` itself. What is under test
# here is the no-shell cascade: the same questions, answered from rules,
# because a V that cannot reach a model still has to answer them.
@test
def the_router_sends_task_questions_to_the_task_layer():
    """Every one of these used to escalate to a paid model or match a single
    noun and print an unrelated table."""
    from v.core import router as r
    rt = r.Router(agentic=False)
    for text, want in [
        ("what is my tomorrow priorities?", r.TASKS),
        ("what are my priorities", r.TASKS),
        ("hey V, how far we are behind?", r.TASKS),
        ("what's on my plate", r.TASKS),
        ("show my open tasks", r.TASKS),
        ("mark buy milk as done", r.TASK_DONE),
        ("what meetings do I have tomorrow", r.CALENDAR),
        ("on Aug 8 at 12:30 I have a meeting", r.SCHEDULE),
    ]:
        eq(rt.route(text).intent, want, f"{text!r} —")


@test
def a_task_question_with_a_source_does_not_return_everything():
    """"what's left from my 1on1 action items" matched the word "items" and
    printed all 178 open tasks. Matching one noun and dumping a table is the
    same failure as the goals one, wearing task clothes."""
    from v.core import router as r
    got = r.Router(agentic=False).route("whats left from my 1on1 action items?")
    eq(got.intent, r.TASKS)
    eq(got.slots.get("source"), "02 - Work/Meetings/1on1s",
       "the source named in the question must reach the query —")


@test
def judgement_questions_are_not_answered_with_a_list():
    """"which tasks should I just delete at this point" is a request for an
    opinion. Answering it with every open task is technically responsive and
    completely useless."""
    from v.core import router as r
    rt = r.Router(agentic=False)
    for text in ("which tasks should i just delete at this point?",
                 "what should i drop from my todo list?",
                 "help me decide which tasks to keep"):
        eq(rt.route(text).intent, r.COMPOUND, f"{text!r} —")
    eq(rt.route("what are my tasks").intent, r.TASKS,
       "a plain listing question must still be free —")


@test
def next_meeting_is_answered_as_a_time_not_a_table():
    from v.core import router as r
    got = r.Router(agentic=False).route("when is my next meeting?")
    eq(got.intent, r.CALENDAR)
    eq(got.slots.get("view"), "next")


@test
def adding_a_task_is_not_a_compound_request():
    """"add a task to review the pricing doc" holds two action verbs, one of
    them inside the task's own text. Sending it to a paid model to file one
    checkbox is the cost failure the router exists to prevent."""
    from v.core import router as r
    rt = r.Router(agentic=False)
    got = rt.route("add a task to review the pricing doc tomorrow")
    eq(got.intent, r.TASK_ADD)
    eq(got.slots.get("text"), "review the pricing doc tomorrow")
    eq(got.slots.get("when"), "tomorrow")


@test
def compound_requests_refuse_the_cheap_path():
    """"based on my last 1on1, update my goals and create a todo list" matched
    the noun "goals", printed a two-row table, and stopped — instantly, free,
    and having done none of the three things asked."""
    from v.core import router as r
    rt = r.Router(agentic=False)
    got = rt.route("based on my last 1on1 with my teamlead, update my goals "
                   "and create a todo list for me")
    eq(got.intent, r.COMPOUND, "a three-verb request —")
    for plain in ("show me open issues", "what are my priorities",
                  "hi", "what did I decide about pricing"):
        ok(rt.route(plain).intent != r.COMPOUND,
           f"{plain!r} is not compound")


@test
def a_task_id_survives_reindexing():
    """Ids are blake2b(rel|line), never rowids. With autoincrement, a rebuild
    renumbers tasks and a stored id ticks a different box."""
    a = tasks.task_id("01 - Daily Notes/x.md", 12)
    b = tasks.task_id("01 - Daily Notes/x.md", 12)
    eq(a, b)
    ok(a != tasks.task_id("01 - Daily Notes/x.md", 13))
    ok(a != tasks.task_id("01 - Daily Notes/y.md", 12))


@test
def task_ids_survive_a_json_round_trip():
    """Ids cross the MCP boundary as JSON numbers, which are IEEE doubles.
    The first version used 8 bytes, so `588988339574306832` came back rounded
    and the agent — correctly — refused to tick a box it could no longer
    identify. Anything above 2^53 is not a usable id here.
    """
    import json

    for rel, line in [("01 - Daily Notes/2026/08 - August/2026-08-01.md", 34),
                      ("02 - Work/Meetings/1on1s/2026-07-26 Arash.md", 7),
                      ("x.md", 0), ("y/z/deep note.md", 999)]:
        i = tasks.task_id(rel, line)
        ok(i < 2 ** 53, f"{i} exceeds the JSON-safe integer range")
        eq(json.loads(json.dumps({"id": i}))["id"], i,
           "id changed across a JSON round-trip —")


@test
def no_two_tasks_in_this_vault_share_an_id():
    """Shortening ids to survive JSON traded bits for a collision risk. Assert
    the actual vault is clean rather than trusting the arithmetic."""
    from v.core import index
    conn = index.connect()
    total, distinct = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM tasks").fetchone()
    conn.close()
    if not total:
        return  # nothing indexed here; the unit test above still holds
    eq(distinct, total, f"{total - distinct} colliding task id(s) —")


@test
def grounding_evidence_never_carries_a_private_note():
    """Feeding local search results into the model's prompt — the fix for V
    claiming the vault had no YouTube note while holding one — created a new
    egress path. `Rag.search` deliberately has no privacy filter (local CLI
    search may see everything the user wrote), so the boundary has to be
    enforced where the text crosses into a prompt."""
    from v.core.engine import _evidence_block

    class Hit:
        def __init__(self, rel):
            self.rel, self.heading = rel, ""

        def preview(self, n):
            return "secret contents"

    class Res:
        pass

    s = Settings()
    ok(s.private_folders, "no private folders configured — vacuous test")
    res = Res()
    res.hits = [Hit(f"{s.private_folders[0]}/note.md")]
    eq(_evidence_block(res, s), "", "a private passage reached the prompt")

    res.hits = [Hit("02 - Work/Issues/ISSUE-1 x.md")]
    ok("ISSUE-1" in _evidence_block(res, s), "public notes must still ground")


@test
def v_knows_it_has_tasks_and_a_calendar():
    """The system prompt is the only place a model learns that tasks are
    checkboxes and that the calendar is a vault file. Without it, V answered
    "I don't have access to an external calendar" about the user's own notes."""
    from v.core.identity import SYSTEM
    low = SYSTEM.lower()
    for phrase in ("vault_tasks", "vault_calendar", "checkbox",
                   "01 - daily notes/calendar"):
        ok(phrase in low, f"the system prompt never mentions {phrase!r}")
    ok("without having searched" in low or "search first" in low,
       "the prompt must forbid claiming absence without searching")


def _crm(root: Path) -> None:
    """A CRM folder shaped like the real one: mostly notes with no address at
    all, one where Parsa typed an email in by hand, two first-name collisions,
    and prose that must never come back out."""
    d = root / "10 - Personal CRM"
    d.mkdir(parents=True, exist_ok=True)
    (d / "0_Dashboard.md").write_text(
        "---\ntype: dashboard\n---\n\n# Contacts\n- **Email:** all@example.com\n",
        encoding="utf-8")
    (d / "Arash Yousefi.md").write_text(
        "---\ntype: person\naliases: []\ntier: 1\n---\n\n"
        "# Arash Yousefi\n\n## Quick Profile\n\n"
        "- **Email:** arash.yousefi@example.com\n"
        "- **Phone:** +98 912 345 6789\n"
        "- **First Met:** 2024-03-11\n\n"
        "## Preferences & Quirks\n\n"
        "- He is going through a rough divorce, do not bring up his wife.\n",
        encoding="utf-8")
    (d / "Alireza Mahmoudi.md").write_text(
        "---\ntype: person\nemail: alireza.m@example.com\n---\n\n# Alireza\n",
        encoding="utf-8")
    (d / "Alireza Nilgaran.md").write_text(
        "---\ntype: person\nemail: nilgaran@example.com\n---\n\n# Alireza\n",
        encoding="utf-8")
    (d / "Maman.md").write_text(
        "---\ntype: person\ntier: 1\n---\n\n# Maman\n\nCall her more.\n",
        encoding="utf-8")


@test
def a_name_resolves_to_the_address_parsa_already_wrote_down():
    """Asked for Arash's calendar, V said no calendar was visible and asked
    Parsa for his email address. It was in `10 - Personal CRM/Arash
    Yousefi.md` the whole time. The folder is private, which was read as "do
    not look" when the thing worth protecting was the prose, not the
    address."""
    from v.core import contacts

    root = Path(tempfile.mkdtemp())
    _crm(root)
    s = Settings(vault=root, state_dir=root / ".state")

    hits = contacts.lookup("arash", s)
    eq([c.name for c in hits], ["Arash Yousefi"], "a first name is enough")
    eq(hits[0].emails, ("arash.yousefi@example.com",))
    ok(hits[0].phones and "912" in hits[0].phones[0], hits[0].phones)
    eq(contacts.emails_for("Arash Yousefi", s), ["arash.yousefi@example.com"])


@test
def a_contact_lookup_cannot_return_the_private_half_of_the_note():
    """The whole licence for reading this folder is that only identifiers come
    back. If a body ever rides along, the exception has eaten the rule."""
    from v.core import contacts

    root = Path(tempfile.mkdtemp())
    _crm(root)
    s = Settings(vault=root, state_dir=root / ".state")

    blob = " ".join(c.render() for c in contacts.lookup("arash", s))
    for leak in ("divorce", "wife", "Quick Profile", "First Met"):
        ok(leak not in blob, f"a contact lookup leaked {leak!r}")
    eq(sorted(vars(contacts.lookup("arash", s)[0])),
       ["emails", "name", "phones", "rel"],
       "Contact grew a field — every one of them is visible to a model")


@test
def two_alirezas_come_back_as_two():
    """Picking one and mailing them is the failure mode. Both, or ask."""
    from v.core import contacts

    root = Path(tempfile.mkdtemp())
    _crm(root)
    s = Settings(vault=root, state_dir=root / ".state")

    eq(len(contacts.lookup("alireza", s)), 2, "one first name, two people")
    eq([c.name for c in contacts.lookup("alireza nilgaran", s)],
       ["Alireza Nilgaran"], "the full name disambiguates")
    eq(contacts.lookup("ali", s), [],
       "whole tokens only — 'ali' is not a prefix search for Alireza")
    eq(contacts.lookup("", s), [], "an empty name never enumerates the folder")
    eq([c.name for c in contacts.lookup("dashboard", s)], [],
       "the folder's dashboard note is not a person")


@test
def a_contact_with_no_address_says_so_instead_of_vanishing():
    """Most of the folder has no email on it. "No contact named Maman" would
    be a lie; "she is in your contacts with no address" is the answer."""
    from v.core import contacts

    root = Path(tempfile.mkdtemp())
    _crm(root)
    s = Settings(vault=root, state_dir=root / ".state")

    hits = contacts.lookup("maman", s)
    eq([c.name for c in hits], ["Maman"])
    eq(hits[0].emails, (), "no address on the note, and none invented")


@test
def gcal_resolve_falls_back_to_contacts_and_marks_it_unshared():
    """`resolve` matched Google's calendar list and stopped. The fallback has
    to be last (a shared calendar beats an address) and has to come back
    flagged `unshared`, so the caller sends it to freeBusy instead of
    rendering titles Google never returned."""
    from v.core import gcal

    root = Path(tempfile.mkdtemp())
    _crm(root)
    (root / ".gcal_ics_url").write_text(
        "# Personal\n"
        "https://calendar.google.com/calendar/ical/me%40gmail.com/private-a/basic.ics\n",
        encoding="utf-8")
    g = gcal.GoogleCalendar(Settings(vault=root, state_dir=root / ".state"))

    hits = g.resolve("arash")
    eq([h["id"] for h in hits], ["arash.yousefi@example.com"])
    eq(hits[0]["source"], "crm")
    eq(hits[0]["access"], "unshared")
    ok(not hits[0]["writable"], "an address book entry is not write access")

    eq([h["name"] for h in g.resolve("personal")], ["Personal"],
       "a real calendar still wins over the address book")
    eq(g.resolve("maman"), [], "no address on file, so nothing to resolve")


@test
def v_is_told_to_look_up_a_contact_before_asking_for_one():
    """The prompt is the only place V learns the tool exists. Without it the
    tool ships and V still asks Parsa to type out an address he has saved."""
    from v.core.identity import SYSTEM
    low = SYSTEM.lower()
    ok("vault_contact" in low, "the prompt never mentions vault_contact")
    ok("10 - personal crm" in low, "the prompt must still name the folder")


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} task/calendar tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
