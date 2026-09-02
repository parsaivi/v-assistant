"""Tests for the MCP seam.

Run:  python3 -m v.tests.test_mcp

Same no-pytest harness as test_core. These tests exist because the MCP layer is
where the safety properties are *enforced* rather than merely intended: if the
privacy filter or the no-raw-write rule regresses here, nothing downstream
catches it — the model just quietly gets more than it should.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
# Never let a test spend plan allowance or 9s on a model call. The
# decider ships enabled, and every fixture here builds a real
# Settings, so this has to be set before v.core is imported.
os.environ["BRAIN_NO_MODEL"] = "1"
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import config, nudges as nudges_mod
from v.mcp import server as srv
from v.mcp.guard import Blocked, Privacy

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


def _tmp_settings():
    """A throwaway state dir. Only the state-file readers need one — every
    other test here runs against the real vault on purpose, because the MCP
    server binds its settings at import."""
    root = Path(tempfile.mkdtemp(prefix="v-mcp-test-"))
    s = config.Settings(vault=root, state_dir=root / "state")
    return root, s


def call(name: str, **kw) -> str:
    """Invoke a tool the way a shell would — through the MCP dispatcher."""
    async def go():
        r = await srv.mcp.call_tool(name, kw)
        # FastMCP returns (content_blocks, structured_output). Older builds
        # returned a result object, or the blocks alone — unwrap whichever
        # arrived rather than assuming, because guessing wrong here silently
        # compares assertions against a list's repr and every error-path test
        # "fails" while the tool is working perfectly.
        c = getattr(r, "content", r)
        if isinstance(c, tuple):
            c = c[0]
        if isinstance(c, list):
            c = c[0]
        return getattr(c, "text", str(c))
    return asyncio.run(go())


# ---------------------------------------------------------------------
@test
def every_tool_is_registered():
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    expected = {
        "vault_query", "vault_search", "vault_read", "vault_nudges",
        "vault_validate", "vault_status", "vault_standup", "vault_reminders",
        "vault_append", "vault_create", "vault_remind", "vault_undo",
    }
    missing = expected - names
    ok(not missing, f"tools missing from the MCP surface: {missing}")


@test
def there_is_no_raw_write_tool():
    """The shell must never get a general-purpose file writer through MCP.

    Writes go through vault_append / vault_create so they stay append-only,
    git-committed and undoable. A tool named write/edit/delete here would be a
    hole straight past vaultfs.

    Three names now legitimately contain "edit"/"rewrite" — EDITH's, added
    when Parsa asked for one agent that can modify existing notes. They are
    not a hole in this rule, they are the exception it now has to state
    precisely: each one goes through `vaultfs` like every other write (per-path
    `[bot]` commit, `undo` reverts it), each one refuses unless the running
    agent's `Persona.edits` is true, and `shells.EDIT_TOOLS` keeps them out of
    every other agent's allowlist entirely. The tests below check all three of
    those, so this one is free to allow the names.
    """
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    # `vault_memory_write` is named for what it writes to — the SQLite memory
    # store — not the vault. Its markdown mirror is rendered by `journal`,
    # which goes through vaultfs like everything else, so it opens no path to a
    # note that vault_append doesn't already own. The substring guard is about
    # a *filesystem* writer, and it stays as tight as it was for those.
    allowed = {"vault_memory_write", "vault_voice_learn",
               "vault_edit", "vault_set_meta", "vault_rewrite"}
    banned = {n for n in names - allowed
              if any(w in n for w in ("write", "edit", "delete", "rewrite", "rm"))}
    ok(not banned, f"raw mutation tools exposed over MCP: {banned}")


@test
def the_edit_tools_refuse_every_agent_but_edith():
    """The second of the two gates, and the one that catches the day somebody
    changes the first. `shells.EDIT_TOOLS` normally keeps these out of the
    allowlist so they are not even visible — but the MCP server can be started
    directly, and then the only thing standing between a prompt and a modified
    note is this check.
    """
    import os
    was = os.environ.get("V_AGENT")
    try:
        for agent, may in (("v", False), ("friday", False), ("", False),
                           ("edith", True)):
            os.environ["V_AGENT"] = agent
            denied = srv._may_edit()
            if may:
                ok(not denied,
                   f"{agent!r} was refused but should be allowed: {denied}")
            else:
                ok(denied, f"{agent or '(unset)'} was allowed to edit")
                ok("append-only" in denied,
                   "the refusal should say what the agent may do instead")
    finally:
        if was is None:
            os.environ.pop("V_AGENT", None)
        else:
            os.environ["V_AGENT"] = was


@test
def the_edit_tools_are_absent_from_a_non_editing_allowlist():
    """The first gate: a tool that is not in `--allowed-tools` cannot be called
    however the model is prompted. This is the guarantee that does not depend
    on anyone following a rule, so it is the one worth pinning."""
    from v.core import shells
    for name in shells.EDIT_TOOLS:
        ok(name not in shells.READ_TOOLS, f"{name} is on the read list")
        ok(name not in shells.WRITE_TOOLS,
           f"{name} is in WRITE_TOOLS, so every writing agent gets it")


@test
def private_folders_are_refused_by_path():
    p = Privacy(config.settings)
    ok(p.folders, "no private folders configured — the guard would be a no-op")
    for folder in p.folders:
        out = call("vault_read", rel=f"{folder}/whatever.md")
        ok(out.startswith("ERROR:"), f"vault_read leaked {folder}: {out[:80]}")
        ok("private" in out.lower(), "refusal should say why")


@test
def private_folders_never_appear_in_search():
    """Checks the *source path* of each hit, not the raw text.

    A public note is allowed to mention "17 - AIT" — `19 - Assistant/Feature
    Brainstorm` has it in a heading. Substring-matching the whole response
    flags that as a breach, and a guard that cries wolf gets ignored. What
    actually matters is that no returned passage *comes from* a private note.
    """
    p = Privacy(config.settings)
    for folder in p.folders:
        term = folder.split(" - ", 1)[-1]
        out = call("vault_search", query_text=term, k=10)
        for line in out.splitlines():
            if not line.startswith("• **"):
                continue
            rel = line[4:].split("**", 1)[0].split(" › ", 1)[0].strip()
            ok(not p.blocks(rel + ".md"),
               f"vault_search returned a passage from {rel}")


@test
def private_folders_never_appear_in_query_results():
    p = Privacy(config.settings)
    for kind in ("people", "overdue_contacts", "birthdays", "orphans"):
        out = call("vault_query", kind=kind, limit=50)
        for folder in p.folders:
            ok(folder not in out,
               f"vault_query({kind}) leaked a path inside {folder}")


@test
def nudges_keep_the_signal_and_drop_the_names():
    """Regression: the first cut of vault_nudges named three CRM contacts."""
    p = Privacy(config.settings)
    raw = srv.E().nudges.all()
    scrubbed = p.scrub_nudges(raw)
    ok(len(scrubbed) == len(raw), "scrubbing must not drop whole nudges")

    text = nudges_mod.digest(scrubbed, max_items=50)
    for folder in p.folders:
        ok(folder not in text, f"nudge digest leaked {folder}")

    # and the headline survives
    private_ones = [n for n in raw
                    if any(p.blocks(l.strip("[]").split("|")[0]) for l in n.links)]
    if private_ones:
        ok(any(n.text in text for n in private_ones),
           "scrubbing removed the actionable half along with the private half")


@test
def unknown_query_kind_fails_loudly():
    out = call("vault_query", kind="not_a_thing")
    ok(out.startswith("ERROR:"), "an unknown kind should be a clear error")
    ok("issues" in out, "the error should list the valid kinds")


@test
def query_beats_grep_is_stated_in_the_descriptions():
    """The routing hint is load-bearing: a model that greps instead of
    querying pays real money to get a worse answer."""
    tools = {t.name: (t.description or "") for t in asyncio.run(srv.mcp.list_tools())}
    ok("FIRST" in tools["vault_query"].upper(),
       "vault_query must tell the model to prefer it over grep")
    ok("only" in tools["vault_append"].lower(),
       "vault_append must state it is the only write path")


@test
def create_refuses_a_schema_violating_note():
    """A note that fails its schema is invisible to the dashboards that query
    it — a silent failure. So it must never reach disk."""
    out = call("vault_create",
               rel="02 - Work/Issues/ISSUE-99999 test.md",
               frontmatter='{"type": "issue", "status": "opened"}',
               body="x")
    ok(out.startswith("ERROR:"), f"bad status was accepted: {out[:120]}")
    ok("nothing written" in out or "schema" in out.lower(),
       "the refusal should name the schema as the reason")
    ok(not (config.settings.vault / "02 - Work/Issues/ISSUE-99999 test.md").exists(),
       "a schema-invalid note was written to disk anyway")


@test
def create_rejects_malformed_frontmatter():
    out = call("vault_create", rel="x.md", frontmatter="{not json", body="")
    ok(out.startswith("ERROR:"), "malformed JSON should be a clear error")


@test
def reads_never_mutate_the_vault():
    """Cheap insurance: a read tool that writes would be caught by git, but
    only after the fact."""
    import subprocess
    root = config.settings.vault
    before = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True).stdout
    for name, kw in [("vault_status", {}), ("vault_nudges", {}),
                     ("vault_validate", {}), ("vault_reminders", {}),
                     ("vault_query", {"kind": "issues"}),
                     ("vault_search", {"query_text": "anything", "k": 2})]:
        call(name, **kw)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           capture_output=True, text=True).stdout
    ok(before == after, "a read-only tool changed the working tree")


@test
def the_two_halves_of_the_privacy_guard_agree():
    """`config.private_folders` seals the MCP surface; the deny rules in
    .claude/settings.json seal Claude Code's own Read/Grep/Glob. They are
    written in two different languages and drift silently — so assert it.
    """
    import json
    p = Privacy(config.settings)
    settings_file = config.settings.vault / ".claude" / "settings.json"
    ok(settings_file.exists(), ".claude/settings.json is missing — Read/Grep "
                               "would reach private folders unguarded")
    deny = json.loads(settings_file.read_text())["permissions"]["deny"]
    for folder in p.folders:
        for tool in ("Read", "Grep", "Glob"):
            rule = f"{tool}(./{folder}/**)"
            ok(rule in deny, f"missing deny rule: {rule}")


@test
def markdown_writes_are_denied_to_the_native_tools():
    """Every note-bearing folder must be unreachable by Write/Edit.

    Asserts the invariant, not a literal pattern: the rule was originally
    `Write(./**/*.md)`, which also caught developer docs — files with no
    append-only guarantee to protect — so it was narrowed to the numbered
    vault folders. What must not change is that a shell cannot write a note
    without going through vaultfs, so this checks coverage folder by folder
    and will fail if a narrowing ever leaves one exposed.
    """
    import fnmatch
    import json

    vault = config.settings.vault
    deny = json.loads(
        (vault / ".claude" / "settings.json").read_text()
    )["permissions"]["deny"]

    def denied(rel: str) -> bool:
        return any(
            r.startswith(("Write(", "Edit("))
            and fnmatch.fnmatch(f"./{rel}", r[r.index("(") + 1:-1])
            for r in deny
        )

    folders = sorted(d.name for d in vault.iterdir()
                     if d.is_dir() and d.name[:2].isdigit())
    ok(folders, "no numbered vault folders found — is this the right vault?")
    for folder in folders:
        for rel in (f"{folder}/note.md", f"{folder}/sub/deep/note.md"):
            ok(denied(rel),
               f"{rel} is writable — a shell could bypass vaultfs and write a "
               f"note with no [bot] commit and no undo")

    # And the narrowing has to have actually happened, or it protects nothing
    # it didn't before while still blocking the code.
    ok(not denied("README.md"),
       "a markdown file outside the numbered folders should not be caught by "
       "the vault guard — if it is, the narrowing never happened")


@test
def every_mcp_tool_is_reachable_by_the_shell():
    """A tool registered here but missing from `shells.READ_TOOLS` /
    `WRITE_TOOLS` cannot be called, and nothing says so.

    That is exactly how it failed: asked to update goals and build a todo
    list, the agent tried `Write`, hit the deny rule, and answered that it had
    no write access to the vault — while `vault_add_task` existed, worked, and
    simply was not on the allowlist. The prompt described a capability the
    process had removed.
    """
    from v.core import shells

    registered = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    # `EDIT_TOOLS` is reachable, just conditionally — `run()` appends it for an
    # agent whose `Persona.edits` is true. It belongs in this set for the same
    # reason the others do: the failure this test exists to catch is a tool
    # that no code path can ever reach, and these have one.
    allowed = {t.split("mcp__vault__")[-1]
               for t in shells.READ_TOOLS + shells.WRITE_TOOLS
                        + shells.EDIT_TOOLS
               if t.startswith("mcp__vault__")}
    missing = registered - allowed
    ok(not missing,
       f"registered on MCP but unreachable by the shell: {sorted(missing)}")

    stale = allowed - registered
    ok(not stale,
       f"allow-listed for the shell but not registered: {sorted(stale)}")


@test
def mutating_tools_are_never_in_the_read_set():
    """`allow_writes=False` is the default posture, and it is only meaningful
    if the read set genuinely cannot change the vault."""
    from v.core import shells

    reads = {t for t in shells.READ_TOOLS if t.startswith("mcp__vault__")}
    mutators = {"vault_append", "vault_create", "vault_remind", "vault_undo",
                "vault_task_done", "vault_add_task", "vault_schedule"}
    leaked = {r for r in reads if r.split("mcp__vault__")[-1] in mutators}
    ok(not leaked, f"write tools in the read-only set: {sorted(leaked)}")


@test
def every_tool_survives_being_called():
    """A smoke call per tool, with arguments that should produce a clean
    refusal rather than work.

    This exists because `vault_task_done` shipped referencing `E().vault`,
    which does not exist on the per-thread bundle — an AttributeError the
    model reported back as "the vault tool server is throwing an internal
    error". Every tool here is reachable by an agent, so every tool has to at
    minimum not explode on its own argument shape.
    """
    cases = [
        ("vault_tasks", {"view": "priorities", "limit": 1}),
        ("vault_tasks", {"view": "backlog"}),
        ("vault_tasks", {"view": "search", "text": "zzz-no-such-task"}),
        ("vault_tasks", {"view": "not_a_view"}),
        ("vault_calendar", {"day": "2099-01-01"}),
        ("vault_calendar", {"day": "not-a-date"}),
        ("vault_task_done", {"task_id": 1}),                 # no such task
        ("vault_task_done", {"task_id": 1, "state": "bogus"}),
        ("vault_schedule", {"text": "no date or time in here"}),
        ("vault_add_task", {"text": "x", "rel": "12 - Brain Dump/x.md"}),
    ]
    for name, kw in cases:
        out = call(name, **kw)
        ok(isinstance(out, str) and out,
           f"{name}({kw}) returned nothing")
        ok("Traceback" not in out and "AttributeError" not in out,
           f"{name}({kw}) raised: {out[:160]}")


@test
def the_write_tools_refuse_before_they_write():
    """Each write tool's failure mode is a message, not an exception, and a
    private path is refused rather than written."""
    ok(call("vault_task_done", task_id=1).startswith("ERROR:"),
       "an unknown task id should be a clear error")
    ok(call("vault_schedule",
            text="lunch sometime").startswith("ERROR:"),
       "an undated event should be refused, not guessed at")
    out = call("vault_add_task", text="x", rel="12 - Brain Dump/secret.md")
    ok(out.startswith("ERROR:") and "private" in out.lower(),
       f"writing into a private folder was not refused: {out[:120]}")


@test
def engines_are_per_thread():
    """sqlite connections are thread-bound; the MCP runtime dispatches tool
    calls on worker threads. Regression for the first version, which shared one
    connection and failed on every call."""
    import threading
    seen = {}

    def grab(key):
        seen[key] = id(srv.E().conn)

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok(seen["a"] != seen["b"], "threads shared one sqlite connection")
    ok(id(srv.E().conn) == id(srv.E().conn), "same thread must reuse its bundle")


# --- Quera ------------------------------------------------------------
@test
def quera_status_reads_and_never_writes():
    """The whole point of the read/write split. `vault_quera_status` must be
    able to answer "are my deadlines current?" on a read-only run, which means
    it may not create a note as a side effect of looking."""
    written = []

    class FakeVault:
        def exists(self, rel):
            return False

        def create(self, *a, **kw):
            written.append(a[0] if a else "?")

    class FakeQuera:
        def configured(self):
            return True

        def fetch_deadlines(self):
            from v.core.quera import QueraAssignment
            return [QueraAssignment(course="DSD", title="Lab 9",
                                    due_at=dt.datetime(2026, 9, 6, 8, 0),
                                    url="https://quera.org/x")]

        def _assignment_note(self, a):
            return f"03 - University/Assignments/{a.title}.md", {}, ""

    real_q, real_v = srv._quera, srv._vault
    srv._quera, srv._vault = (lambda: FakeQuera()), FakeVault()
    try:
        out = call("vault_quera_status")
    finally:
        srv._quera, srv._vault = real_q, real_v

    ok(not written, f"status wrote notes: {written}")
    ok("Lab 9" in out, f"the live deadline should be listed: {out[:200]}")
    ok("NOT IN THE VAULT" in out,
       "a deadline with no note must be called out, not silently counted")
    ok("out of sync" in out, "and the summary line must say so")


@test
def quera_status_says_who_can_fix_an_expired_session():
    """V cannot re-authorise Quera — there is no browser login here, only a
    cookie Parsa pastes. Saying "I have no access" and stopping is the failure
    this tool was added to end; naming the one command that fixes it is the
    behaviour that replaces it."""
    class NoCookie:
        def configured(self):
            return False

    real = srv._quera
    srv._quera = lambda: NoCookie()
    try:
        out = call("vault_quera_status")
    finally:
        srv._quera = real
    ok("quera auth" in out, f"the fix must be named: {out[:200]}")
    ok("nightly job" in out, "and the automatic job still reported")


@test
def quera_descriptions_carry_the_two_things_v_got_wrong():
    """Both failures on record were the model reasoning from an absence: it
    had no tool, so it invented "no Quera access is set up for me"; and it read
    an empty widget as a broken sync. Neither is fixable by a tool that does
    not say so in its own description."""
    tools = {t.name: (t.description or "")
             for t in asyncio.run(srv.mcp.list_tools())}
    st = tools["vault_quera_status"]
    ok("06:45" in st, "the description must state the sync is automatic")
    ok("only deadlines still open" in st,
       "and that an absent assignment is not a failed sync")
    ok("EDITH" in tools["vault_quera_sync"],
       "sync must not be mistaken for a way to mark something done")


@test
def the_scheduler_last_run_is_readable_without_an_engine():
    """`vault_quera_status` reports when the nightly job fired, and it runs in
    the MCP process, which has no Engine and has no business building one."""
    from v.core import scheduler as sched
    root, s = _tmp_settings()
    try:
        eq_none = sched.last_run(sched.QUERA_JOB, s)
        ok(eq_none is None, "no state file must read as 'never', not crash")
        (s.state_dir).mkdir(parents=True, exist_ok=True)
        (s.state_dir / "scheduler.json").write_text(
            json.dumps({sched.QUERA_JOB: "2026-09-02T11:09:21"}))
        got = sched.last_run(sched.QUERA_JOB, s)
        ok(got == dt.datetime(2026, 9, 2, 11, 9, 21), f"got {got!r}")
        (s.state_dir / "scheduler.json").write_text("{not json")
        ok(sched.last_run(sched.QUERA_JOB, s) is None,
           "a corrupt state file must not take the tool down with it")
    finally:
        shutil.rmtree(root)


# ---------------------------------------------------------------------
def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} MCP tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
