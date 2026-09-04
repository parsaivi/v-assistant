"""Tests for the boundaries between V, Friday and EDITH.

Run:  python3 -m v.tests.test_agents

A persona used to be a voice. It is now an agent — its own memory, its own
reach, its own model floor, its own posture on writes — and every one of those
is a boundary that fails *silently* when it breaks. A memory leaking between
agents looks like V being well-informed. A surface gate that only guards the
switch looks like it works right up until a Telegram message resolves to EDITH.
So each boundary gets a test that asserts the wrong thing cannot happen, not
merely that the right thing does.
"""

from __future__ import annotations

import os
os.environ["BRAIN_NO_MODEL"] = "1"
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import config, memory as memory_mod, persona, router, traits

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


def stores() -> tuple[memory_mod.MemoryStore, ...]:
    """V, Friday and EDITH over one database, as the engine builds them."""
    root = Path(tempfile.mkdtemp(prefix="v-agents-"))
    s = config.Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return tuple(
        memory_mod.MemoryStore(s, conn, scope=p.scope, reads=p.reads_scopes)
        for p in (persona.V, persona.FRIDAY, persona.EDITH))


# --- memory is per agent ---------------------------------------------------
@test
def each_agent_keeps_its_own_beliefs():
    v, friday, _ = stores()
    v.remember("Call them merge requests, never MRs.",
               kind=memory_mod.PREFERENCE, why="he corrected me twice")
    friday.remember("Lead a status line with the blocked item.",
                    kind=memory_mod.PREFERENCE, why="he said so on the repl")
    eq(len(v.all()), 1, "V sees Friday's preference:")
    eq(len(friday.all()), 1, "Friday sees V's preference:")
    ok("merge requests" in v.all()[0].text)
    ok("blocked" in friday.all()[0].text)


@test
def a_preference_taught_to_one_agent_does_not_reach_the_others_prompt():
    """The consequence that matters. `ALWAYS_ON` puts every preference into
    every run regardless of topic, so an unscoped store would have made one
    correction binding on all three characters at once."""
    v, friday, _ = stores()
    v.remember("Answer in one line unless asked for detail.",
               kind=memory_mod.PREFERENCE, why="he said so")
    ok("one line" in v.context_block("anything at all"),
       "V should be injecting its own preference")
    ok("one line" not in friday.context_block("anything at all"),
       "V's preference leaked into Friday's prompt")


@test
def edith_reads_the_others_and_writes_only_her_own():
    """The one asymmetry in the system. She is useless as a local deep-dive
    tool if she cannot see what V knows, and dangerous if she can quietly
    author what V will later act on."""
    v, friday, edith = stores()
    a = v.remember("Parsa works at Divar on Pricing.",
                   kind=memory_mod.FACT, why="he said so")
    b = friday.remember("Sprint reviews land on Wednesdays.",
                        kind=memory_mod.FACT, why="he said so")
    c = edith.remember("He wants raw query output here, not a summary.",
                       kind=memory_mod.PREFERENCE, why="said so in the repl")
    seen = {m.id for m in edith.all()}
    ok({a.id, b.id, c.id} <= seen, "EDITH cannot see what V and Friday know")
    eq([m.id for m in edith.all(own_only=True)], [c.id],
       "EDITH claims ownership of another agent's memory:")
    # …and none of the four mutators may touch a row she only reads.
    ok(not edith.forget(a.id), "EDITH forgot one of V's memories")
    ok(v.get(a.id).active, "V's memory was deactivated by EDITH")
    ok(edith.revise(a.id, text="something else") is None,
       "EDITH rewrote one of V's memories")
    eq(v.get(a.id).text, "Parsa works at Divar on Pricing.",
       "V's memory text was changed by EDITH:")
    before = v.get(a.id).strength
    edith.reinforce(a.id, "used it")
    eq(v.get(a.id).strength, before,
       "EDITH strengthened one of V's beliefs by reading it:")


@test
def forming_a_belief_never_retires_another_agents():
    """Supersession is a write. EDITH reads V's store, so without `own_only`
    in `_related` she would retire V's beliefs as a side effect of forming her
    own — quiet authorship by a different route."""
    v, _, edith = stores()
    original = v.remember("Always call them merge requests, never MRs.",
                          kind=memory_mod.PREFERENCE, why="he corrected me")
    edith.remember("Always call them merge requests, never MRs, in full.",
                   kind=memory_mod.PREFERENCE, why="same rule, her own copy")
    ok(v.get(original.id).active,
       "EDITH's near-identical belief superseded V's")
    eq(v.get(original.id).superseded_by, None,
       "V's memory was pointed at a row in another agent's scope:")


@test
def v_cannot_see_edith_at_all():
    """The read is one-way in both directions of the sentence: EDITH reads V,
    and V does not read EDITH. Otherwise the local-only agent's notes reach
    Telegram by the back door."""
    v, _, edith = stores()
    hers = edith.remember("Skip the preamble entirely here.",
                          kind=memory_mod.PREFERENCE, why="said so locally")
    eq(v.get(hers.id), None, "V can read EDITH's memory by id:")
    ok("preamble" not in v.context_block("anything"),
       "EDITH's preference reached V's prompt")


@test
def rows_written_before_the_column_existed_belong_to_v():
    """The migration. Every memory in the store today was written when V was
    the only occupant, so they are V's — and a default that dropped them into
    no scope at all would silently empty the memory Parsa has been building."""
    root = Path(tempfile.mkdtemp(prefix="v-agents-old-"))
    s = config.Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # the pre-migration table, without `agent`
    conn.executescript("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            subject TEXT, text TEXT NOT NULL, why TEXT, source TEXT,
            confidence REAL DEFAULT 0.7, strength REAL DEFAULT 1.0,
            used_count INTEGER DEFAULT 0, created REAL, last_used REAL,
            active INTEGER DEFAULT 1, superseded_by INTEGER);
    """)
    conn.execute("INSERT INTO memories (kind,text,why,active,strength,created,"
                 "last_used) VALUES ('preference','Be brief.','he said so',"
                 "1,1.0,0,0)")
    conn.commit()
    v = memory_mod.MemoryStore(s, conn, scope="v")
    eq(len(v.all()), 1, "the pre-migration memory vanished:")
    eq(v.all()[0].agent, "v", "an old row landed outside V's scope:")
    _, _, edith = stores()
    shutil.rmtree(root, ignore_errors=True)


# --- reach -----------------------------------------------------------------
@test
def edith_is_reachable_from_the_terminal_only():
    eq(persona.EDITH.surfaces, (persona.TUI,))
    ok(persona.EDITH.allows(persona.TUI))
    for surface in (persona.CLI, persona.TELEGRAM, persona.PHONE,
                    persona.SCHEDULER):
        ok(not persona.EDITH.allows(surface),
           f"EDITH is reachable from {surface}")
    for p in (persona.V, persona.FRIDAY):
        for surface in persona.SURFACES:
            ok(p.allows(surface), f"{p.key} is blocked on {surface}")


@test
def v_is_reachable_everywhere_because_everything_falls_back_to_it():
    """The gate returns a refusal rather than raising, which is only safe
    because there is always somebody left to answer."""
    eq(persona.V.surfaces, ())
    eq(persona.get(persona.DEFAULT).key, "v")


@test
def a_summons_off_the_terminal_never_becomes_the_stored_default():
    """Saying "Friday" to the phone is a wake word, not a preference. It used
    to write `persona: friday` into `state/config.json`, so one utterance to
    the Android client silently re-cast the TUI, Telegram and the scheduler."""
    eq(persona.PERSISTS_FROM, (persona.TUI,))
    for surface in (persona.CLI, persona.TELEGRAM, persona.PHONE,
                    persona.SCHEDULER):
        ok(surface not in persona.PERSISTS_FROM,
           f"a summons from {surface} would rewrite the config")


@test
def summoning_from_the_phone_holds_for_the_session_and_no_longer():
    """The behaviour, not just the constant: the switch must still take effect
    on the surface that asked for it, and must not follow him home."""
    from v.core.engine import Engine
    from v.tests.test_core import make_vault
    root, s = make_vault()
    cfg = s.state_dir / "config.json"
    try:
        e = Engine(s, surface=persona.PHONE)
        try:
            switched, refusal = e.switch_agent("friday")
            ok(switched and not refusal, f"the phone refused Friday: {refusal!r}")
            eq(e.persona.key, "friday", "the summons must hold for the session")
            ok("friday" not in (cfg.read_text() if cfg.exists() else ""),
               "a phone summons wrote itself into the stored default")
        finally:
            e.close()
        e = Engine(s, surface=persona.TUI)
        try:
            eq(e.persona.key, "v", "the phone's summons leaked to the terminal")
            e.switch_agent("friday")
            ok("friday" in cfg.read_text(),
               "a summons typed at the terminal must still persist")
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


@test
def the_shared_prompt_teaches_no_honorific_of_its_own():
    """`_OPERATING` goes to every agent, so an address written into its worked
    examples is one character's manner demonstrated to all of them. Friday
    answered "Report 6 md is written, sir" — the example's own sentence with
    the number swapped — two turns after calling him "boss"."""
    from v.core import identity
    body = identity._OPERATING.lower()
    ok("{address}" in identity._OPERATING,
       "the shared prompt no longer takes the persona's address")
    for honorific in ("sir", "boss"):
        ok(f", {honorific}" not in body,
           f"the shared operating rules address him as {honorific!r}")
    ok(f", {identity.ADDRESS.lower()} " in identity.SYSTEM.lower(),
       "the built prompt lost the live address")


# --- naming ----------------------------------------------------------------
@test
def calling_a_name_summons_it_and_talking_about_one_does_not():
    r = router.Router()
    for text, want in (("friday", "friday"), ("EDITH", "edith"),
                       ("hey edith", "edith"), ("V", "v"),
                       ("jarvis", "jarvis"), ("edith, are you there", "edith")):
        route = r.route(text)
        eq(route.intent, router.SUMMON, f"{text!r} did not summon:")
        eq(route.slots["name"], want, f"{text!r} summoned the wrong name:")
    # A sentence *about* an agent is not a summons. Getting this wrong means a
    # question is silently answered by a different agent with different memory.
    for text in ("ask friday what she thinks", "what should friday do",
                 "V, what does my week look like", "is edith any good"):
        route = r.route(text)
        ok(route.intent != router.SUMMON,
           f"{text!r} was read as a summons")


@test
def jarvis_still_resolves_to_v():
    """An existing config naming the old key must keep resolving to the same
    agent rather than silently becoming a different one."""
    for name in ("jarvis", "JARVIS", "J.A.R.V.I.S.", "jarv"):
        eq(persona.get(name).key, "v", f"{name!r} stopped resolving:")
    eq(persona.JARVIS.key, "v", "the JARVIS export moved:")
    eq(persona.get("nonsense").key, "v", "an unknown name should give V:")


# --- capability ------------------------------------------------------------
@test
def edith_carries_a_model_floor_and_the_others_do_not():
    eq(persona.EDITH.model, "opus")
    eq(persona.EDITH.effort, "high")
    for p in (persona.V, persona.FRIDAY):
        eq(p.model, "", f"{p.key} pins a model, overriding the router's tiers:")


@test
def direct_writes_never_extend_past_the_vault():
    """The one place this design could quietly become dangerous. EDITH skips
    the show-and-wait step because vault writes revert; nothing about that
    reasoning applies to an email, and both the persona note and her own
    prompt have to keep saying so."""
    eq(persona.EDITH.writes, persona.DIRECT)
    for p in (persona.V, persona.FRIDAY):
        eq(p.writes, persona.CONFIRM, f"{p.key} writes without confirming:")
    text = persona.EDITH.voice_prompt
    ok("permission model" in text)
    ok("draft an email and stop" in text)
    ok("never relaxes a rule" in " ".join(persona.EDITH.core).lower())


@test
def every_agent_has_a_distinct_memory_scope():
    scopes = [p.scope for p in persona.PERSONAS.values()]
    eq(len(set(scopes)), len(scopes), f"two agents share a scope ({scopes}):")
    for p in persona.PERSONAS.values():
        ok(p.scope not in p.reads_scopes,
           f"{p.key} lists its own scope as a foreign read")


@test
def the_agent_reaches_the_mcp_subprocess():
    """The isolation's weakest joint. The MCP server is a *separate process*
    launched from `.mcp.json`, so it cannot see `engine.persona`; without the
    environment variable it opens at the default scope and every tool call
    EDITH makes writes into V's beliefs — the boundary enforced one level up
    and quietly undone one level down.
    """
    import os as _os
    import subprocess as _sp
    probe = (
        "from v.mcp import server;"
        "e = server.E();"
        "print(e.memory.scope, ','.join(e.memory.reads))"
    )
    root = Path(__file__).resolve().parents[2]
    for want_agent, want_scope, want_reads in (
            ("edith", "edith", "v,friday"), ("friday", "friday", ""),
            ("", "v", "")):
        env = dict(_os.environ, PYTHONPATH=str(root))
        if want_agent:
            env["V_AGENT"] = want_agent
        else:
            env.pop("V_AGENT", None)
        out = _sp.run([sys.executable, "-c", probe], capture_output=True,
                      text=True, env=env, cwd=str(root))
        if out.returncode:
            raise AssertionError(f"probe failed: {out.stderr[-400:]}")
        scope, reads = (out.stdout.strip().split(" ") + [""])[:2]
        eq(scope, want_scope, f"V_AGENT={want_agent!r} opened the wrong scope:")
        eq(reads, want_reads, f"V_AGENT={want_agent!r} got the wrong reads:")


@test
def the_engine_tells_the_shell_which_agent_is_asking():
    """The other half of the same seam, on the Python side: the model floor
    and the agent name have to leave the engine, or the subprocess is told
    nothing and falls back to defaults that look correct in isolation."""
    import inspect
    from v.core import engine as engine_mod, hermes, shells
    src = inspect.getsource(engine_mod.Engine._escalate)
    ok("agent=self.persona.scope" in src,
       "the engine no longer names its agent to the shell")
    ok("model=self.persona.model" in src,
       "the engine no longer passes its model floor")
    for fn in (hermes.Hermes.ask, shells.ClaudeCodeShell.run):
        ok("agent" in inspect.signature(fn).parameters,
           f"{fn.__qualname__} dropped the agent parameter")


# --- editing ---------------------------------------------------------------
@test
def only_edith_may_modify_an_existing_note():
    eq(persona.EDITH.edits, True)
    for p in (persona.V, persona.FRIDAY):
        eq(p.edits, False, f"{p.key} can modify notes:")


@test
def the_edit_tools_are_not_in_any_ordinary_allowlist():
    """The guarantee that holds without anyone following a rule: a tool absent
    from `--allowed-tools` cannot be called however the model is prompted."""
    from v.core import shells
    for name in shells.EDIT_TOOLS:
        ok(name not in shells.READ_TOOLS + shells.WRITE_TOOLS,
           f"{name} is reachable by every writing agent")
    ok(set(shells.EDIT_TOOLS) == {
        "mcp__vault__vault_edit", "mcp__vault__vault_set_meta",
        "mcp__vault__vault_rewrite"},
       "the edit surface changed without this test noticing")


@test
def an_edit_refuses_what_it_cannot_place_exactly():
    """The two refusals that keep an edit from being a guess.

    Not-found means the model is working from a version of the note that does
    not exist, which is when an edit does the most damage. Multiple matches
    means it cannot know which was meant, and taking the first is a coin flip
    on Parsa's data. Both have to raise rather than do something reasonable.
    """
    import subprocess
    from v.core import vaultfs
    root = Path(tempfile.mkdtemp(prefix="v-edit-"))
    (root / "04 - Gym").mkdir(parents=True)
    note = root / "04 - Gym" / "Plan.md"
    note.write_text("---\ntype: plan\n---\n\n## Week\n\n- squat 100kg\n"
                    "- bench 80kg\n- squat 100kg\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    s = config.Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()
    v = vaultfs.Vault(s)
    rel = "04 - Gym/Plan.md"
    before = note.read_text()

    for bad, why in (("- deadlift 140kg", "text that is not in the note"),
                     ("- squat 100kg", "text that appears twice")):
        try:
            v.edit(rel, bad, "- squat 110kg")
        except vaultfs.VaultError as exc:
            ok(str(exc), f"the refusal for {why} should explain itself")
        else:
            raise AssertionError(f"edit accepted {why}")
    eq(note.read_text(), before, "a refused edit still touched the file:")

    # …and a unique match goes through, changing only what was named.
    v.edit(rel, "- bench 80kg", "- bench 85kg")
    after = note.read_text()
    ok("- bench 85kg" in after, "the edit did not apply")
    eq(after.count("- squat 100kg"), 2, "the edit disturbed other lines:")
    ok(after.startswith("---\ntype: plan"), "frontmatter was damaged")

    # An edit is a [bot] commit like every other write, so `undo` reaches it.
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root,
                         capture_output=True, text=True).stdout
    ok(log.strip().startswith(s.commit_prefix),
       f"an edit was not committed as a bot change: {log!r}")
    shutil.rmtree(root, ignore_errors=True)


@test
def an_edit_cannot_reach_a_private_note():
    """The privacy guard is orthogonal to the edit gate and has to stay that
    way: EDITH being allowed to modify notes says nothing about which notes
    she may see, and `10 - Personal CRM` is not one of them."""
    import inspect
    from v.mcp import server as srv
    for fn in (srv.vault_edit, srv.vault_set_meta, srv.vault_rewrite):
        src = inspect.getsource(fn)
        ok("_privacy.check(rel)" in src,
           f"{fn.__name__} does not run the privacy guard")
        ok("_may_edit()" in src, f"{fn.__name__} does not check the agent")


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
