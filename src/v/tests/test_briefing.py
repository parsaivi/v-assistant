"""Tests for the standing briefing, the brag drafter, and template filling.

Run:  python3 -m v.tests.test_briefing

These three ship together because they are the same bet: V should do the
recurring thing on its own rather than wait to be asked. That bet is only worth
taking if it stays quiet when there is nothing to say and writes nothing it
cannot justify — so most of what is frozen here is the *restraint*, not the
feature. A thin morning that stays silent, a typo fix that never becomes a brag
entry, a note V creates being indistinguishable from one Obsidian created.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Never spend plan allowance, and never pop a real desktop notification at
# whoever is running the suite — `briefing.run` genuinely calls notify-send.
os.environ["BRAIN_NO_MODEL"] = "1"
os.environ["BRAIN_NO_NOTIFY"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import (brag, briefing, index, notes, router, schema,
                            templates)
from v.core.config import Settings
from v.core.engine import Engine

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


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


DAILY_TEMPLATE = """---
date: <% tp.file.title %>
week: "<% tp.date.now("GGGG-[W]WW", 0, tp.file.title, "YYYY-MM-DD") %>"
month: "<% tp.date.now("YYYY-MM", 0, tp.file.title, "YYYY-MM-DD") %>"
quarter: "<% tp.date.now("YYYY-[Q]Q", 0, tp.file.title, "YYYY-MM-DD") %>"
mood:
energy:
tags:
  - daily-note
---

# <% tp.date.now("dddd, MMMM D, YYYY", 0, tp.file.title, "YYYY-MM-DD") %>

## 📅 Calendar
_No events scheduled._

## Tasks
### Work
- [ ]

## Work Log
### Blockers
-

## Day life

## End of Day
"""

PERSON_TEMPLATE = """---
type: person
tier:
frequency_days:
last_contact:
tags:
  - person
---

# {{title}}

## 👤 Quick Profile

## 🗓 Interaction Log
"""

BRAG_TEMPLATE = """---
type: brag
title:
date: "{{date:YYYY-MM-DD}}"
category:
project: Pricing
impact:
tags:
  - work/brag
---

# 🏆 {{title}}

## What I did

## Why it mattered / Impact

## Evidence
- **MRs:**

## Praise / Recognition

## Reflection
"""


def make_vault(*, with_templates: bool = True) -> tuple[Path, Settings]:
    root = Path(tempfile.mkdtemp(prefix="brain-brief-"))
    for folder in ("01 - Daily Notes/2026/08 - August", "02 - Work/Issues",
                   "02 - Work/Merge Requests", "02 - Work/Brag Document",
                   "04 - Gym/Workouts", "10 - Personal CRM", "05 - Templates"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    if with_templates:
        (root / "05 - Templates/Daily Note.md").write_text(
            DAILY_TEMPLATE, encoding="utf-8")
        (root / "05 - Templates/Template_Person.md").write_text(
            PERSON_TEMPLATE, encoding="utf-8")
        (root / "05 - Templates/Brag Entry.md").write_text(
            BRAG_TEMPLATE, encoding="utf-8")

    # git needs at least one tracked file; a vault built without templates
    # would otherwise have nothing to make the base commit from.
    (root / "01 - Daily Notes/2026/08 - August/.gitkeep").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    s = Settings(vault=root, state_dir=root / ".state")
    s.embedding_backend = "tfidf"
    s.ensure_dirs()
    index.rebuild(s, full=True)
    return root, s


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def merged_mr(iid: int, title: str, *, body: str = "", day: str = "2026-08-03") -> str:
    return (f'---\ntype: merge-request\nmr-id: {iid}\ntitle: "{title}"\n'
            f'project: Pricing\nstatus: merged\ncreated: "2026-07-01"\n'
            f'merged: "{day}"\ntags:\n  - work/mr\n---\n\n{body}\n')


# --- templates -------------------------------------------------------
@test
def moment_tokens_match_what_the_templates_use():
    """The four formats that actually appear in `05 - Templates/`."""
    day = dt.date(2026, 8, 5)
    eq(templates.moment_format(day, "GGGG-[W]WW"), "2026-W32")
    eq(templates.moment_format(day, "YYYY-[Q]Q"), "2026-Q3")
    eq(templates.moment_format(day, "YYYY-MM"), "2026-08")
    eq(templates.moment_format(day, "dddd, MMMM D, YYYY"),
       "Wednesday, August 5, 2026")


@test
def a_daily_note_v_creates_matches_the_one_obsidian_creates():
    """The drift this fixes: V's scaffold had no Calendar and no Blockers, so
    the calendar sync had nowhere to write on any day V got there first."""
    root, s = make_vault()
    try:
        e = Engine(s)
        rel = e.capture.ensure_daily(dt.date(2026, 8, 5))
        body = e.vault.read(rel).body
        for heading in ("## 📅 Calendar", "### Blockers", "## Day life",
                        "## End of Day"):
            ok(heading in body, f"template section missing: {heading}")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_filled_template_leaves_no_token_behind():
    """An unrendered `{{…}}` in frontmatter is a schema *error* — it is the
    single biggest source of notes that vanish from every dashboard."""
    root, s = make_vault()
    try:
        e = Engine(s)
        rel = e.capture.ensure_daily(dt.date(2026, 8, 5))
        note = e.vault.read(rel)
        ok("<%" not in note.render() and "{{" not in note.render(),
           "a template token survived into the note")
        eq(str(note.meta["week"]), "2026-W32")
        issues = [i for i in schema.validate_note(note, settings=s)
                  if i.level == "error"]
        eq(issues, [], "a note V created must be schema-clean")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_vault_without_templates_still_gets_its_note():
    """The fallback body. A missing template is never a reason to lose a
    capture — and every test vault in the other suites has no templates."""
    root, s = make_vault(with_templates=False)
    try:
        e = Engine(s)
        rel = e.capture.ensure_daily(dt.date(2026, 8, 5))
        ok(e.vault.exists(rel))
        ok("Day life" in e.vault.read(rel).body)
        e.close()
    finally:
        shutil.rmtree(root)


@test
def interactions_join_the_log_the_template_made():
    """V used to append CRM interactions under a heading called
    "Interactions", which no person note has — so every one bolted a second
    log onto the bottom of the note instead of joining the first."""
    root, s = make_vault()
    try:
        e = Engine(s)
        route = router.Route(router.CAPTURE_PERSON, 0.99, "test")
        e.capture.capture("Had coffee about the pricing work", route,
                          when=dt.datetime(2026, 8, 3, 15, 0),
                          confirmed=True, extra={"name": "Ali"})
        # …and again, so the second one has an existing note to join.
        e.capture.capture("Reviewed his MR", route,
                          when=dt.datetime(2026, 8, 4, 15, 0),
                          confirmed=True, extra={"name": "Ali"})
        body = (root / "10 - Personal CRM/Ali.md").read_text(encoding="utf-8")
        ok("## Interactions" not in body, "a stray second log was created")
        ok(body.count("🗓 Interaction Log") == 1)
        ok("Reviewed his MR" in body, "the second interaction joined the log")
        e.close()
    finally:
        shutil.rmtree(root)


# --- the standing briefing -------------------------------------------
@test
def signal_ignores_the_things_that_are_always_true():
    """Broken wikilinks and open issues are the background state of a vault.
    Interrupting for them is how a briefing teaches you to dismiss it."""
    eq(briefing.signal({"nudges": 9, "events": 0, "tasks": 0,
                        "reminders": 0, "urgent": 0}), 0)
    ok(briefing.signal({"events": 1, "urgent": 0, "tasks": 0,
                        "reminders": 0}) > 0, "a meeting is worth a push")
    ok(briefing.signal({"events": 0, "urgent": 1, "tasks": 0,
                        "reminders": 0}) > 0, "so is something due today")
    eq(briefing.signal({"events": 0, "urgent": 0, "tasks": 1,
                        "reminders": 0}), 0, "one task is not news")
    ok(briefing.signal({"events": 0, "urgent": 0, "tasks": 2,
                        "reminders": 0}) > 0, "a shortlist is")


@test
def a_thin_morning_is_never_pushed():
    root, s = make_vault()
    try:
        e = Engine(s)
        brief = briefing.run(e, now=dt.datetime(2026, 8, 3, 10, 0))
        ok(brief.thin, f"an empty vault scored {brief.signal}")
        eq(brief.delivered, False)
        ok("thin" in brief.reason)
        # Recorded anyway, so `--status` can explain the silence and so a
        # second tick at 10:30 doesn't try again.
        eq(briefing.last_run(s)["delivered"], False)
        e.close()
    finally:
        shutil.rmtree(root)


@test
def the_brief_lands_in_todays_note_exactly_once():
    root, s = make_vault()
    try:
        e = Engine(s)
        e.reminders.add("standup", dt.datetime.now().replace(hour=23, minute=0))
        e.reminders.add("review the MR",
                        dt.datetime.now().replace(hour=23, minute=30))
        now = dt.datetime.now().replace(hour=10, minute=0)
        first = briefing.run(e, now=now)
        ok(first.delivered, f"not delivered: {first.reason}")
        ok(any("daily note" in c for c in first.channels))

        rel = e.capture.daily_path(now.date())
        body = e.vault.read(rel).body
        eq(body.count("Morning brief"), 1)

        second = briefing.run(e, now=now.replace(minute=30))
        eq(second.delivered, False)
        ok("already delivered" in second.reason)
        eq(e.vault.read(rel).body.count("Morning brief"), 1,
           "the cron entry and the scheduler must not both push")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def quiet_mode_silences_the_standing_push():
    """`/quiet` is thrown in the REPL and has to be honoured by cron, which is
    a different process — so it is read from disk, never cached."""
    root, s = make_vault()
    try:
        e = Engine(s)
        e.reminders.add("standup", dt.datetime.now().replace(hour=23, minute=0))
        e.reminders.add("ship it", dt.datetime.now().replace(hour=23, minute=5))
        # Derived from the instant being checked, not from wall-clock now:
        # `config` pins the process to Asia/Tehran, so a `quiet_until` built
        # from `now()` and compared against a hardcoded 10:00 only sits in the
        # future when the pinned clock happens to read past 07:00.
        now = dt.datetime.now().replace(hour=10)
        (s.state_dir / "surface.json").write_text(json.dumps(
            {"quiet_until": (now + dt.timedelta(hours=3)).isoformat()}),
            encoding="utf-8")
        brief = briefing.run(e, now=now)
        eq(brief.delivered, False)
        eq(brief.reason, "quiet mode")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def the_cron_entry_pins_the_interpreter_it_was_installed_with():
    root, s = make_vault()
    try:
        line = briefing.cron_line(s)
        ok(line.startswith("0 10 * * *"), line)
        ok(sys.executable in line, "a bare `python3` in cron is a coin flip")
        ok(briefing.CRON_MARKER in line, "without the marker, installing twice "
                                         "stacks two entries")
    finally:
        shutil.rmtree(root)


# --- brag drafting ----------------------------------------------------
@test
def the_first_run_seeds_instead_of_filing_a_hundred_entries():
    root, s = make_vault()
    write(root, "02 - Work/Merge Requests/MR-1 old work.md",
          merged_mr(1, "old work", body="## Summary\n\n" + "detail " * 40))
    try:
        index.rebuild(s, full=True)
        e = Engine(s)
        report = e.brag.run()
        eq(report["seeded"], 1)
        eq(report["drafted"], [])
        eq(len(list((root / "02 - Work/Brag Document").glob("*.md"))), 0)

        # …and the run after the seed drafts nothing either, because nothing
        # new has closed since.
        eq(e.brag.run()["drafted"], [])
        e.close()
    finally:
        shutil.rmtree(root)


@test
def only_closures_with_something_in_them_become_entries():
    root, s = make_vault()
    write(root, "02 - Work/Merge Requests/MR-1 real work.md",
          merged_mr(1, "add the image coverage rpc",
                    body="## Summary\n\n" + "It wires the coverage check "
                                            "into the worker. " * 6))
    write(root, "02 - Work/Merge Requests/MR-2 typo.md",
          merged_mr(2, "fix typo in readme",
                    body="## Summary\n\n" + "a typo " * 40))
    write(root, "02 - Work/Issues/ISSUE-9 empty.md",
          '---\ntype: issue\nissue-id: 9\ntitle: something\nproject: Pricing\n'
          'status: closed\npriority: low\ncreated: "2026-07-01"\n'
          'closed: "2026-08-03"\n---\n\n## Description\n\n## Activity Log\n')
    try:
        index.rebuild(s, full=True)
        e = Engine(s)
        report = e.brag.run(backfill=True)
        wrote = [d["rel"] for d in report["drafted"]]
        eq(len(wrote), 1, f"drafted: {wrote}")
        ok("MR-1" in wrote[0])
        why = {sk["rel"].split("/")[-1]: sk["why"] for sk in report["skipped"]}
        ok(any("routine" in w for k, w in why.items() if "MR-2" in k),
           f"a typo fix must not become an achievement: {why}")
        ok(any("nothing recorded" in w for k, w in why.items()
               if "ISSUE-9" in k), f"{why}")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_drafted_entry_is_schema_valid_and_cites_its_evidence():
    root, s = make_vault()
    write(root, "02 - Work/Merge Requests/MR-985 coverage.md",
          merged_mr(985, "add the image coverage rpc",
                    body="## Summary\n\n"
                         + "It wires the coverage check into the worker. " * 6
                         + "\n\n## Comments\n\n### 💬 @amir · 2026-08-01\n"
                           "> GG, this is clean\n"))
    try:
        index.rebuild(s, full=True)
        e = Engine(s)
        report = e.brag.run(backfill=True)
        eq(len(report["drafted"]), 1, str(report))
        note = e.vault.read(report["drafted"][0]["wrote"])

        eq(note.meta["type"], "brag")
        eq(str(note.meta["date"]).strip('"'), "2026-08-03")
        eq(note.meta["category"], "recognition", "praise in review")
        # Blank on purpose: the line only Parsa can write stays unwritten
        # rather than being guessed at.
        eq(note.meta["impact"], None)
        ok("draft" in [str(t) for t in note.meta["tags"]])
        ok("MR-985" in note.body, "an entry with no evidence is unusable")
        ok("GG, this is clean" in note.body, "the praise should be quoted")
        eq([i for i in schema.validate_note(note, settings=s)
            if i.level == "error"], [])
        e.close()
    finally:
        shutil.rmtree(root)


@test
def work_already_in_the_brag_document_is_not_drafted_again():
    """Checked against the vault, not the state file — deleting `brag.json`
    must not produce a second copy of every entry."""
    root, s = make_vault()
    write(root, "02 - Work/Merge Requests/MR-985 coverage.md",
          merged_mr(985, "add the image coverage rpc",
                    body="## Summary\n\n"
                         + "It wires the coverage check into the worker. " * 6))
    write(root, "02 - Work/Brag Document/Coverage RPC.md",
          '---\ntype: brag\ntitle: Coverage RPC\ndate: "2026-08-03"\n'
          'category: delivery\ntags:\n  - work/brag\n---\n\n'
          '## Evidence\n- [[02 - Work/Merge Requests/MR-985 coverage]]\n')
    try:
        index.rebuild(s, full=True)
        e = Engine(s)
        report = e.brag.run(backfill=True)
        eq(report["drafted"], [])
        ok(any("already in the brag document" in sk["why"]
               for sk in report["skipped"]), str(report))
        e.close()
    finally:
        shutil.rmtree(root)


@test
def drafts_are_flagged_but_never_urgent():
    """The drafter's pitch is that evidence collects itself. A daily nag to go
    and finish the entries by hand would undo exactly that."""
    root, s = make_vault()
    write(root, "02 - Work/Brag Document/Something.md",
          '---\ntype: brag\ntitle: Something\ndate: "2026-08-03"\n'
          'category: delivery\ntags:\n  - work/brag\n  - draft\n---\n\nBody.\n')
    try:
        index.rebuild(s, full=True)
        e = Engine(s)
        found = e.nudges.brag_drafts(dt.date(2026, 8, 3))
        eq(len(found), 1)
        eq(found[0].urgency, 0)
        eq(briefing.signal({"nudges": 1, "events": 0, "tasks": 0,
                            "reminders": 0, "urgent": 0}), 0,
           "an FYI nudge must never be the reason a morning is interrupted")
        eq(len(e.brag.pending_drafts()), 1)
        e.close()
    finally:
        shutil.rmtree(root)


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
