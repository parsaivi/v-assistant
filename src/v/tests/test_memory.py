"""Tests for V's memory, its identity, and the free tiers around them.

Run:  python3 -m v.tests.test_memory

The theme of this file is regressions that were *silent*. Every one of these
covers a failure the previous build had where nothing errored, nothing logged,
and the answer looked entirely reasonable — a count of zero, a greeting that
cost four cents, a preference that was stored and then never reached a prompt.
Those are the expensive ones, so they get tests.
"""

from __future__ import annotations

import shutil
import subprocess
import os
# Never let a test spend plan allowance or 9s on a model call. The
# decider ships enabled, and every fixture here builds a real
# Settings, so this has to be set before v.core is imported.
os.environ["BRAIN_NO_MODEL"] = "1"
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import (config, identity, index, memory as memory_mod,
                            query, router, shells)
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


def make_vault() -> tuple[Path, config.Settings]:
    root = Path(tempfile.mkdtemp(prefix="v-test-"))
    for d in ("01 - Daily Notes/2026/08 - August", "02 - Work/Issues",
              "02 - Work/Merge Requests", "04 - Gym/Workouts",
              "09 - Anti-Library/05 - Locations", "19 - Assistant/V/Logs"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # GitLab's vocabulary, on purpose — this is the exact shape that made the
    # old build answer "0 open issues" while holding two of them.
    (root / "02 - Work/Issues/ISSUE-1 alpha.md").write_text(
        '---\ntype: issue\nissue-id: 1\ntitle: alpha\nproject: Pricing\n'
        'status: opened\npriority: high\ncreated: "2026-07-01"\n---\n\nBody.\n',
        encoding="utf-8")
    (root / "02 - Work/Issues/ISSUE-2 beta.md").write_text(
        '---\ntype: issue\nissue-id: 2\ntitle: beta\nproject: Pricing\n'
        'status: closed\npriority: low\ncreated: "2026-07-02"\n---\n\nBody.\n',
        encoding="utf-8")
    (root / "02 - Work/Merge Requests/MR-9 gamma.md").write_text(
        '---\ntype: merge-request\nmr-id: 9\ntitle: gamma\nproject: Pricing\n'
        'status: opened\ncreated: "2026-07-03"\n---\n\nBody.\n',
        encoding="utf-8")
    # `wifi: good` is what the vault actually writes; `wifi:` blank is the
    # other real case. Neither used to survive a wifi=True filter.
    (root / "09 - Anti-Library/05 - Locations/Luna.md").write_text(
        '---\ntype: place\nname: Luna\ncategory: cafe\nlocation: "1,2"\n'
        'city: Tehran\nrating: 8\nwifi: good\nnoise: low\n'
        'outdoor-seating: true\n---\n\nGood.\n', encoding="utf-8")
    (root / "09 - Anti-Library/05 - Locations/Lido.md").write_text(
        '---\ntype: place\nname: Lido\ncategory: cafe\nlocation: "3,4"\n'
        'city: Tehran\nrating: 10\nwifi:\nnoise: low\n'
        '---\n\nAlso good.\n', encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    s = config.Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()
    index.rebuild(s, full=True)
    return root, s


# --- the vocabulary bug ----------------------------------------------
@test
def gitlab_status_vocabulary_does_not_hide_work():
    """`opened` and `open` are the same thing, and 40 issues went missing.

    This is the worst failure the system can have: a wrong answer that reads
    like a fact. "0 open issues" is not a hedge — you act on it.
    """
    root, s = make_vault()
    try:
        q = query.QueryEngine(s)
        eq(len(q.issues(status="open").rows), 1, "`opened` must match `open`")
        eq(len(q.issues(status="closed").rows), 1)
        eq(len(q.merge_requests(status="open").rows), 1)
        # And the other direction, since a human types either.
        eq(len(q.issues(status="opened").rows), 1)
        eq(len(q.blocked().rows), 1, "blocked() filters MRs on status too")
    finally:
        shutil.rmtree(root)


@test
def status_normalisation_is_symmetric():
    n = query.normalise_status
    eq(n("opened"), "open")
    eq(n("Open"), "open")
    eq(n("in review"), "in-review")
    eq(n("WIP"), "in-progress")
    eq(n("something-else"), "something-else", "unknown values pass through")
    eq(n(None), "")


# --- tri-state frontmatter -------------------------------------------
@test
def an_unrecorded_flag_excludes_nothing():
    """`wifi: good` is truthy and `wifi:` blank is unknown, not false.

    Folding both to False made "cafés with wifi" return zero while the vault
    held two cafés with wifi.
    """
    root, s = make_vault()
    try:
        res = query.QueryEngine(s).places(wifi=True)
        names = sorted(r.get("name") for r in res.rows)
        eq(names, ["Lido", "Luna"], "'good' is truthy; blank is unknown")
        ok("not recorded" in res.answer,
           "an unverified match must say so rather than imply it was checked")
        # Verified matches sort above unverified ones.
        eq(res.rows[0].get("name"), "Luna")
    finally:
        shutil.rmtree(root)


@test
def tri_state_reads_the_vaults_actual_vocabulary():
    eq(query._tri("good"), True)
    eq(query._tri("true"), True)
    eq(query._tri(""), None)
    eq(query._tri(None), None)
    eq(query._tri("false"), False)
    eq(query._tri("no"), False)


# --- staleness --------------------------------------------------------
@test
def a_changed_vault_is_detected_and_refreshed():
    """The old build answered from whatever was last indexed, and never said so."""
    root, s = make_vault()
    try:
        ok(not index.is_stale(s), "nothing changed yet")
        (root / "02 - Work/Issues/ISSUE-3 delta.md").write_text(
            '---\ntype: issue\nissue-id: 3\ntitle: delta\nproject: Pricing\n'
            'status: opened\npriority: low\ncreated: "2026-07-04"\n---\n\nB.\n',
            encoding="utf-8")
        ok(index.is_stale(s), "a new note makes the mirror stale")
        index.refresh_if_stale(s)
        ok(not index.is_stale(s))
        eq(len(query.QueryEngine(s).issues(status="open").rows), 2)
    finally:
        shutil.rmtree(root)


@test
def a_deleted_note_is_detected_even_though_mtimes_are_unchanged():
    """Deleting a file touches no mtime, so a mtime-only check misses it —
    which is how a café deleted days earlier kept showing up in results."""
    root, s = make_vault()
    try:
        (root / "09 - Anti-Library/05 - Locations/Lido.md").unlink()
        ok(index.is_stale(s), "a deletion must be noticed")
        index.refresh_if_stale(s)
        eq(len(query.QueryEngine(s).places().rows), 1)
    finally:
        shutil.rmtree(root)


# A store over a throwaway vault, for the tests that only need the store and
# would otherwise be four lines of tempdir bookkeeping each. Cleaned up in
# `main()`.
_TMP: list[Path] = []


def _store() -> memory_mod.MemoryStore:
    root, s = make_vault()
    _TMP.append(root)
    return memory_mod.MemoryStore(s)


# --- memory -----------------------------------------------------------
@test
def a_correction_supersedes_rather_than_accumulating():
    """Two live memories about the same thing means the injected context
    contradicts itself, and V follows whichever it read last."""
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        first = m.remember("always call them merge requests",
                           kind=memory_mod.PREFERENCE, why="told")
        second = m.remember("always call them merge requests, never MRs",
                            kind=memory_mod.PREFERENCE, why="corrected")
        live = m.all()
        eq(len(live), 1, "the subject slot holds one belief at a time")
        eq(live[0].id, second.id)
        old = m.get(first.id)
        ok(not old.active)
        eq(old.superseded_by, second.id,
           "the old belief keeps a pointer to what replaced it")
    finally:
        shutil.rmtree(root)


@test
def restating_a_memory_reinforces_it_instead_of_duplicating():
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        a = m.remember("keep answers short and skip the preamble",
                       kind=memory_mod.PREFERENCE)
        b = m.remember("keep answers short, skip the preamble",
                       kind=memory_mod.PREFERENCE)
        eq(a.id, b.id, "a restatement is the same memory")
        eq(len(m.all()), 1)
        ok(m.get(a.id).strength > 1.0, "and it should be believed more, not less")
    finally:
        shutil.rmtree(root)


@test
def contradiction_weakens_and_eventually_retires_a_memory():
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        mem = m.remember("file gym notes in the daily note",
                         kind=memory_mod.PREFERENCE)
        m.contradict(mem.id, "he said they go in 04 - Gym")
        ok(m.get(mem.id).strength < 1.0)
        m.contradict(mem.id)
        ok(not m.get(mem.id).active, "repeated contradiction retires it")
        # Retired, not erased — the record of what V used to think is the
        # thing you want when V is behaving strangely.
        ok(m.get(mem.id) is not None)
        events = [e["event"] for e in m.history(mem.id)]
        ok("contradicted" in events)
    finally:
        shutil.rmtree(root)


@test
def unused_memories_fade_out_of_the_prompt_before_they_are_deleted():
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        mem = m.remember("prefers Persian dates in the brief",
                         kind=memory_mod.PREFERENCE)
        # Backdate its last use well past the decay horizon.
        long_ago = time.time() - 200 * 86400
        m.conn.execute("UPDATE memories SET last_used=?, created=? WHERE id=?",
                       (long_ago, long_ago, mem.id))
        m.conn.commit()
        faded = m.get(mem.id)
        ok(faded.effective_strength() < memory_mod.MIN_INJECT_STRENGTH,
           "an untouched memory stops being injected")
        ok(faded.active, "but it is still there to be revived or read")
        eq(m.context_block("anything"), "",
           "a faded memory must not reach the prompt")
    finally:
        shutil.rmtree(root)


@test
def behavioural_memories_ride_along_regardless_of_topic():
    """"Be brief" shares no words with the question it governs, so a purely
    keyword-matched memory would never be injected where it matters."""
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        m.remember("answer in at most two sentences",
                   kind=memory_mod.PREFERENCE)
        m.remember("Kian is the Pricing tech lead", kind=memory_mod.FACT)
        block = m.context_block("when did I last squat")
        ok("two sentences" in block, "preferences are always on")
        ok("Kian" not in block, "unrelated facts are not")
        ok("Kian" in m.context_block("who is Kian"), "related ones are")
    finally:
        shutil.rmtree(root)


@test
def no_phrase_can_write_to_memory():
    """The regex layer is gone, and this is what replaced it: nothing.

    `_CORRECTION_RE` matched `^no[,.\s]`, so "no i want you to actually search
    and document famous vulnerabilities so i can learn them" was stored as a
    permanent correction and injected into every later prompt as a standing
    instruction. `_PREFERENCE_RE` matched "i want you to", so the same sentence
    had a second way in. Two of the store's two memories arrived like this.

    A prefix cannot tell a lesson from a retry. Deciding that is the model's
    job now — `vault_memory_write` — and `read_command` only reads commands.
    """
    for line in ("what did I decide about pricing?",
                 "bench 3x8 at 80kg felt strong",
                 "hi",
                 "no i want you to actually search and document famous "
                 "vulnerabilities so i can learn them",
                 "no, they're in 04 - Gym not the daily note",
                 "always call them MRs, not merge requests",
                 "remember that Pricing is the main project"):
        ok(memory_mod.read_command(line) is None,
           f"{line!r} is content — no pattern may store it")


@test
def memory_commands_are_recognised_as_commands():
    eq(memory_mod.read_command("/forget 4"), ("forget", 4))
    eq(memory_mod.read_command("/memory"), ("list", None))
    eq(memory_mod.read_command("what do you know about me"), ("list", None))


@test
def corrections_are_not_always_on():
    """A correction earns its place in the prompt by matching the topic.

    They used to bypass `recall()`'s relevance scoring entirely (10.0 +
    strength), which meant one bad capture became a standing instruction on
    every call about every subject. A correction general enough to apply always
    is a preference, and that is what the model is told to write.
    """
    ok(memory_mod.CORRECTION not in memory_mod.ALWAYS_ON)
    ok(memory_mod.PREFERENCE in memory_mod.ALWAYS_ON,
       "'be brief' shares no keywords with the question it governs")

    store = _store()
    store.remember("gym notes belong in 04 - Gym, not the daily note",
                   kind=memory_mod.CORRECTION, why="he moved one back")
    store.remember("answer in at most three sentences",
                   kind=memory_mod.PREFERENCE, why="he said so")

    squats = store.context_block("how much did I squat last week?")
    ok("three sentences" in squats, "a preference applies whatever the topic")
    ok("04 - Gym" not in squats,
       "an unrelated correction must stay out of the prompt")
    ok("04 - Gym" in store.context_block("where do my gym notes go?"),
       "...but a matching one must reach it")


@test
def injection_is_not_use():
    """Being shown to the model is not evidence a memory was worth showing.

    `context_block` called `mark_used`, which set `last_used=now` — and
    `effective_strength` measures idleness from `last_used`. So an always-on
    memory reset its own decay clock every time it was injected and could never
    fade: strength 1.00 forever, `used 9x` meaning "pasted into nine prompts".
    Rule 4 was unreachable code.
    """
    store = _store()
    m = store.remember("answer in at most three sentences",
                       kind=memory_mod.PREFERENCE, why="he said so")
    old = store.get(m.id).last_used
    store.context_block("anything at all")
    after = store.get(m.id)
    eq(after.last_used, old, "injection must not touch the decay clock")
    eq(after.used_count, 1, "...but it is still counted")

    store.reinforce(m.id, "a turn nobody corrected")
    ok(store.get(m.id).last_used > old, "reinforcement is what moves it")


@test
def a_memory_can_be_revised_in_place():
    """Rewording a belief must not cost its history or its earned strength."""
    store = _store()
    m = store.remember("pricing stuff matters", kind=memory_mod.FACT,
                       why="he mentioned it")
    store.reinforce(m.id, "used and not corrected")
    strong = store.get(m.id).strength

    revised = store.revise(m.id, text="Pricing is the project that matters most "
                                      "to Parsa at Divar")
    eq(revised.id, m.id, "revising keeps the id")
    eq(revised.strength, strong, "...and the strength it earned")
    ok("Pricing is the project" in revised.text)
    ok(store.get(m.id).active, "a revision is not a supersede")


@test
def a_correction_only_touches_what_it_is_about():
    """The bug this covers was silent and total.

    `_teach` picked contradiction targets with `recall()`, which returns every
    behavioural memory regardless of topic — that is exactly what makes "be
    brief" reach a question about squats. So a correction about where gym notes
    go retired an unrelated preference about answer length, reported it as
    replaced, and looked like the feature working.

    That code path is gone with the rest of the regex teach layer; the property
    it got wrong is now `remember()`'s alone, keyed on subject overlap, and is
    tested here at the store where it actually lives.
    """
    store = _store()
    store.remember("always answer in at most two sentences",
                   kind=memory_mod.PREFERENCE, why="he said so")
    store.remember("Kian is the Pricing tech lead",
                   kind=memory_mod.FACT, why="he said so")
    store.remember("gym notes belong in 04 - Gym, not the daily note",
                   kind=memory_mod.CORRECTION, why="he moved one back")

    texts = [m.text for m in store.all()]
    ok(any("two sentences" in t for t in texts),
       "an unrelated preference must survive an unrelated correction")
    ok(any("Kian" in t for t in texts), "an unrelated fact must survive too")
    eq(len(texts), 3, "three subjects, three memories")


@test
def restating_a_belief_still_retires_its_near_duplicates():
    """Restatement returned early, so an older phrasing of the same belief
    stayed live beside it — two versions of one instruction in the prompt."""
    root, s = make_vault()
    try:
        m = memory_mod.MemoryStore(s)
        old = m.remember("put gym stuff in 04 - Gym not the daily note",
                         kind=memory_mod.CORRECTION)
        # Close enough to supersede, not close enough to be a restatement.
        mid = m.remember("gym stuff goes in 04 - Gym and not in the daily note",
                         kind=memory_mod.CORRECTION)
        ok(not m.get(old.id).active, "the near-duplicate should have been retired")
        # Now restate the survivor verbatim: it must reinforce, not fork.
        again = m.remember("gym stuff goes in 04 - Gym and not in the daily note",
                           kind=memory_mod.CORRECTION)
        eq(again.id, mid.id)
        eq(len(m.all()), 1, "one belief, one row")
    finally:
        shutil.rmtree(root)


@test
def naming_a_note_is_a_free_lookup_not_a_capture():
    """"ISSUE-139" scored nothing, landed in the ambiguity prompt, and offered
    to file the identifier as a lyric."""
    root, s = make_vault()
    try:
        eq(router.Router().route("ISSUE-1").intent, router.QUERY)
        e = Engine(s)
        try:
            resp = e.handle("ISSUE-1")
            eq(resp.cost, 0.0)
            eq(resp.route, "structured:note")
            ok("alpha" in resp.text, resp.text)
            missing = e.handle("ISSUE-9999")
            ok("No ISSUE-9999" in missing.text,
               "a missing note must say so, not escalate")
            eq(missing.cost, 0.0)
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


# --- the free tiers ---------------------------------------------------
@test
def smalltalk_never_reaches_a_model():
    """Every one of these used to cost allowance. "how are you" scored as a
    question, matched no schema, and escalated."""
    r = router.Router()
    for line in ("hi", "hey!", "good morning", "thanks", "thank you",
                 "how are you?", "who are you", "what's your name",
                 "سلام", "ممنون", "bye", "good night"):
        route = r.route(line)
        eq(route.intent, router.SMALLTALK, f"{line!r} routed to {route.intent}")


@test
def teaching_is_no_longer_a_routing_decision():
    """"always call them MRs" used to be routed `TEACH` on the word "always".

    The same rule read "i want you to actually search and document famous
    vulnerabilities" as a standing instruction, because both open with a
    phrase from the same list. These now reach the model, which stores the
    lesson through `vault_memory_write` if there is one — and answers the
    request either way, which the old path never did.
    """
    r = router.Router(agentic=True)
    for line in ("always call them MRs, not merge requests",
                 "remember that Kian is the tech lead",
                 "never put gym notes in the daily note",
                 "no, that's wrong"):
        eq(r.route(line).intent, router.AGENT, f"{line!r}")

    # The commands survive, because a command is not an interpretation.
    eq(r.route("/memory").intent, router.RECALL)
    eq(r.route("/forget 2").intent, router.TEACH)


@test
def the_engine_answers_smalltalk_and_meta_for_nothing():
    root, s = make_vault()
    try:
        e = Engine(s)
        try:
            for line in ("hi", "thanks", "who are you", "how many notes do I have"):
                resp = e.handle(line)
                eq(resp.cost, 0.0, f"{line!r} must be free")
                ok(not resp.route.startswith("agent:"),
                   f"{line!r} escalated to a model: {resp.route}")
                ok(resp.text.strip(), f"{line!r} produced nothing")
            ok("V" in e.handle("who are you").text, "V should know its own name")
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


@test
def a_stored_memory_reaches_the_prompt_and_can_be_forgotten():
    """The half of the memory loop that stays local, end to end.

    Writing is the model's now (`vault_memory_write`), so this stores the way
    that tool does and checks the rest still holds: a memory that never reaches
    a prompt is not a memory, `/memory` shows it, and `/forget N` removes it.
    Those three are what `_teach` is left owning.
    """
    root, s = make_vault()
    try:
        e = Engine(s)
        try:
            e.memory.remember("always answer in at most two sentences",
                              kind=memory_mod.PREFERENCE,
                              why="he said so in conversation",
                              source="observed")
            ok("two sentences" in e.memory.context_block("anything about work"),
               "a stored preference that never reaches a prompt is not a memory")

            listed = e.handle("/memory")
            eq(listed.cost, 0.0)
            ok("two sentences" in listed.text)

            mid = e.memory.all()[0].id
            gone = e.handle(f"/forget {mid}")
            eq(gone.route, "memory:forget")
            eq(len(e.memory.all()), 0)
            ok("two sentences" not in
               e.memory.context_block("anything about work"),
               "a forgotten memory must stop being injected")
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


@test
def the_cheapest_model_is_the_floor_of_the_ladder():
    """Haiku 4.5 is the cheapest model available; nothing may sit below it."""
    cheapest = min(shells.MODEL_PRICES.items(), key=lambda kv: kv[1][0])
    eq(cheapest[0], "claude-haiku-4-5")
    s = config.Settings()
    for tier in (shells.TRIVIAL, shells.LIGHT):
        model, _ = shells.tier_settings(tier, s)
        eq(shells.resolve_model(model), "claude-haiku-4-5",
           f"the {tier} tier must use the cheapest model")
    trivial_in, _ = shells.price_of(shells.tier_settings(shells.TRIVIAL, s)[0])
    normal_in, _ = shells.price_of(shells.tier_settings(shells.NORMAL, s)[0])
    ok(trivial_in < normal_in, "the cheap tier must actually be cheaper")


@test
def trivial_work_is_tiered_below_light():
    eq(shells.weigh("rephrase this in one sentence"), shells.TRIVIAL)
    eq(shells.weigh("fix the grammar here"), shells.TRIVIAL)
    eq(shells.weigh("when did I last squat"), shells.LIGHT)
    eq(shells.weigh("what did I decide about pricing and why?"), shells.NORMAL)
    eq(shells.weigh("audit every note for schema errors"), shells.HEAVY)


# --- identity ---------------------------------------------------------
@test
def v_has_exactly_one_name():
    """It used to be Jarvis in the system prompt, "Brain assistant" in the
    REPL and jarvis-vault over MCP — three answers to one question."""
    eq(identity.NAME, "V")
    ok(identity.SYSTEM.startswith("You are V,"))
    ok("Jarvis" not in identity.SYSTEM)
    server = (Path(__file__).resolve().parents[1] / "mcp/server.py").read_text()
    ok("jarvis" not in server.lower(), "the MCP server still calls itself Jarvis")


@test
def a_persona_carries_its_voice_all_the_way_to_the_speaker():
    """Adding Friday should mean adding one entry to `PERSONAS` and nothing
    else. That only holds if every character is complete on its own — a
    prompt, the free-path lines, and a voice to read them in — so this checks
    the shape rather than Jarvis's particular words.

    The last assertion is the one that will actually fail one day: a persona
    added with a voice id its model does not accept looks fine until the
    first voice note, which arrives as `Unavailable` and degrades to Piper
    silently. Only the id's *shape* can be checked offline; the id itself is
    verified against the model's `supported_voices` when it is chosen.
    """
    from v.core import persona, tts

    ok(persona.DEFAULT in persona.PERSONAS, "the default must exist")
    for key, p in persona.PERSONAS.items():
        eq(p.key, key, "a persona's key must match how it is registered")
        ok("## Voice" in p.voice_prompt, f"{key} has no voice section")
        ok(p.address, f"{key} does not know what to call him")
        for flavour in ("greeting", "thanks", "how", "who", "night"):
            ok(flavour in p.smalltalk, f"{key} cannot say {flavour}")
        # Every template has to survive `line()` with the fields the engine
        # passes, or a greeting becomes a KeyError on the free path.
        eq(p.line("greeting", name="V", when="Good evening", tail="Nothing."),
           p.smalltalk["greeting"].format(address=p.address, name="V",
                                          when="Good evening", tail="Nothing."))
        ok(p.line("something nobody implemented", name="V"),
           "an unknown flavour must still say something")
        ok(p.spoken.model and p.spoken.piper,
           f"{key} has no model to speak through and no local fallback")
        # Exactly one of the two, never both and never neither: a preset id
        # names a voice the provider already has, a reference clip carries
        # one we brought. Sending both makes Fish ignore the clip silently,
        # which is the worst of the three outcomes because it still speaks.
        ok(bool(p.spoken.voice) != bool(p.spoken.reference),
           f"{key} must have either a preset voice or a reference clip")
        if p.spoken.reference:
            ok(p.spoken.reference.endswith((".mp3", ".wav")),
               f"{key}'s reference should name an audio file")

    # An unknown name costs the character, never the assistant.
    eq(persona.get("friday-but-typoed").key, persona.DEFAULT)
    eq(persona.get("").key, persona.DEFAULT)
    r = tts.RemoteSynthesizer()
    ok(r.model, "the active persona must resolve a model")
    ok(r.voice or r.reference, "and either a preset voice or a clip to clone")


@test
def the_voice_reaches_the_prompt_and_the_free_paths():
    """The voice is only real if it survives both routes.

    `SYSTEM` covers the model path, but greetings, thanks and sign-offs never
    reach a model — they are literal strings in the engine, and if those drift
    back to "Morning." then V sounds like one assistant when he thinks and a
    different one when he doesn't.
    """
    ok(identity.VOICE in identity.SYSTEM, "the voice is not in the prompt")
    # Operating rules first: if the two ever disagree, correctness was read
    # before charm.
    ok(identity.SYSTEM.index("You are V,") < identity.SYSTEM.index(
        identity.VOICE), "the voice must come after the operating rules")
    ok(identity.PRINCIPAL in identity.VOICE and "one person" in identity.VOICE,
       "V must know there is exactly one person he talks to")

    root, s = make_vault()
    try:
        e = Engine(s)
        try:
            # The engine's own persona, not `identity.ADDRESS` — that one is
            # snapshotted at import from the *live* config, so once Parsa
            # switched his own to Friday this asserted "boss" against a
            # fixture vault answering as V, and failed on his machine only.
            for line in ("hi", "thanks", "good night", "how are you"):
                said = e.handle(line).text
                ok(e.persona.address.lower() in said.lower(),
                   f"{line!r} answered out of voice: {said!r}")
                ok("!" not in said, f"{line!r} used an exclamation mark")
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


@test
def the_codes_block_stays_empty_until_a_code_earns_its_place():
    """An empty "## Codes" heading in the prompt invites the model to invent
    entries to fill it, so the section has to be absent, not blank."""
    if not identity.CODES:
        ok("## Codes" not in identity.SYSTEM,
           "there are no codes, so the prompt should not mention any")
    else:
        for phrase in identity.CODES:
            ok(phrase in identity.SYSTEM, f"code {phrase!r} never reaches V")


@test
def the_skills_page_is_generated_from_live_code():
    """A hand-maintained capability list rots the first time a feature moves."""
    root, s = make_vault()
    try:
        e = Engine(s)
        try:
            caps = identity.capabilities(e)
            flat = " ".join(i["what"] for g in caps for i in g["items"])
            from v.mcp.server import QUERY_KINDS
            from v.core import nudges as nudges_mod
            ok(f"{len(QUERY_KINDS)} kinds" in flat,
               "the query count must come from the code, not a comment")
            ok(f"{len(nudges_mod.NudgeEngine.CHECKS)} vault-state checks" in flat,
               "the nudge count must come from the code")
        finally:
            e.close()
    finally:
        shutil.rmtree(root)


@test
def v_writes_its_own_notes_into_the_vault():
    root, s = make_vault()
    try:
        e = Engine(s)
        try:
            e.journal.refresh_all(capabilities=identity.capabilities(e),
                                  goals=identity.GOALS,
                                  memory_markdown=e.memory.export_markdown())
            for name in ("README.md", "Skills.md", "Goals.md", "Memory.md"):
                p = root / "19 - Assistant/V" / name
                ok(p.exists(), f"{name} was not written")
                ok(p.read_text(encoding="utf-8").strip(), f"{name} is empty")
            e.journal.decide("use SQLite for memory", "SQLite in assistant.db",
                             because="it is already open and already backed up",
                             alternatives="a JSON file")
            text = (root / "19 - Assistant/V/Decisions.md").read_text(encoding="utf-8")
            ok("use SQLite for memory" in text)
            ok("already backed up" in text, "a decision without its reason is a log line")
        finally:
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
    for tmp in _TMP:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
