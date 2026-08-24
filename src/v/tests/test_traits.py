"""Tests for the half of V's character that grows.

Run:  python3 -m v.tests.test_traits

Two things are being defended here, and they are not the same thing.

The first is the plumbing: a trait is a memory of kind 'voice', so it has to
inherit supersession and decay *and* stay out of the block memory renders,
because a habit of speech filed under "Facts about him and his work" reads as a
claim about Parsa rather than an instruction to V.

The second is the guard rails, which are the only reason this feature is safe
to ship. An assistant that learns its manner from one person converges on
agreeing with him unless something structural stops it. The structure is: a cap
on how much prompt the learned half can occupy, and a constitution rendered
after it that says it wins. Both are asserted below, because both are the sort
of thing a later refactor silently drops.
"""

from __future__ import annotations

import os
os.environ["BRAIN_NO_MODEL"] = "1"
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import config, memory as memory_mod, persona, traits as traits_mod

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


def store() -> traits_mod.TraitStore:
    """An in-memory store, no vault, no index. Traits touch neither."""
    root = Path(tempfile.mkdtemp(prefix="v-traits-"))
    s = config.Settings(vault=root, state_dir=root / ".state")
    s.ensure_dirs()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return traits_mod.TraitStore(memory_mod.MemoryStore(s, conn),
                                 persona.V)


# --- plumbing --------------------------------------------------------------
@test
def test_a_trait_needs_its_evidence():
    """The one mechanical bar, and the reason it is this one.

    `memory.py` documents at length how a regex that read intent off a message
    prefix stored a request as a standing instruction. The same trap is open
    here and worse, because nothing in a transcript says "he liked that reply"
    — so the write is the model's judgement. What can be checked without
    reading intent is whether anything was *cited*, and an uncited trait is one
    the model made up.
    """
    t = store()
    try:
        t.learn("Skip the honorific in the morning brief.", why="")
    except ValueError as exc:
        ok("evidence" in str(exc), "the refusal should say what is missing")
    else:
        raise AssertionError("a trait with no evidence was accepted")
    eq(len(t.all()), 0, "the refused trait was stored anyway:")


@test
def test_a_revised_habit_retires_the_one_it_replaces():
    """Inherited from the memory store, and load-bearing: two live traits that
    contradict each other put both in the prompt and V picks whichever it read
    last. Overlap here is 0.62 — above the supersede bar, below the restate
    one — so the second is a correction of the first, not a repeat of it."""
    t = store()
    first = t.learn("Say sir sparingly, at most once a reply.",
                    why="he said it was showing up three times")
    second = t.learn("Say sir sparingly, never more than once in a reply.",
                     why="he tightened the wording")
    live = t.all()
    eq(len(live), 1, "the corrected trait is still live alongside its fix:")
    eq(live[0].id, second.id, "the wrong one of the pair survived:")
    old = t.memories.get(first.id)
    ok(not old.active and old.superseded_by == second.id,
       "the retired trait lost its pointer to what replaced it, which is the "
       "thing that explains V's behaviour later")


@test
def test_restating_a_habit_reinforces_it_rather_than_duplicating():
    """Every "yes, exactly that" is otherwise another row, and the block grows
    until the learned half is most of what describes the character."""
    t = store()
    first = t.learn("Skip the honorific in the morning brief.",
                    why="he reads it on his phone")
    second = t.learn("Skip the honorific in the morning brief",
                     why="he said it again")
    eq(first.id, second.id, "a restatement should return the same row")
    eq(len(t.all()), 1, "a restated trait was stored twice:")
    ok(t.all()[0].strength > 1.0, "a restatement should have reinforced it")


@test
def test_traits_stay_out_of_the_memory_block():
    """The whole reason they are rendered elsewhere. A voice trait in
    `context_block` arrives under "Facts about him and his work", which reads
    as a claim about Parsa rather than an instruction about wording."""
    t = store()
    t.learn("Skip the honorific in the morning brief.",
            why="he reads it on his phone and called it padding")
    t.memories.remember("Parsa works at Divar on the Pricing team.",
                        kind=memory_mod.FACT, why="he said so")
    # The query has to share a word with the fact — `recall` scores factual
    # memories on overlap, so "what am I working on" matches nothing here.
    block = t.memories.context_block("how is Pricing going")
    ok("Divar" in block, "the ordinary memory should still be injected")
    ok("honorific" not in block,
       "a voice trait leaked into the memory block")


@test
def test_a_faded_trait_stops_being_injected_before_it_is_forgotten():
    """Rule 4 of the memory design, applied to character: V should be shaped by
    the last few months, not by everything it was ever told. A habit nobody has
    reinforced since spring should stop *acting* long before it stops
    existing — otherwise `v persona` cannot explain why V went quiet on it."""
    t = store()
    old = time.time() - 200 * 86400
    m = t.learn("Open the brief with the weather.",
                why="he asked for it once in March", now=old)
    t.memories.conn.execute(
        "UPDATE memories SET last_used=?, created=? WHERE id=?",
        (old, old, m.id))
    t.memories.conn.commit()
    eq(len(t.all()), 1, "the trait should still exist:")
    eq(len(t.live()), 0, "a trait idle for 200 days is still being injected:")
    ok("below the line" in "\n".join(t.describe()),
       "`v persona` should say why it stopped applying")


# --- the guard rails -------------------------------------------------------
@test
def test_the_learned_half_is_capped():
    """The cap is the reason a growing personality cannot swamp the shipped
    one. Without it the block grows with use until the character Parsa chose is
    a minority of the text describing it."""
    t = store()
    # Distinct vocabulary on purpose: near-identical traits would supersede
    # each other and never reach the cap, which is the store working, not the
    # cap working.
    habits = [
        "Skip the honorific in the morning brief.",
        "Give gym numbers in kilograms, never pounds.",
        "Quote spending in toman with a thousands separator.",
        "Name merge requests by id before title.",
        "Put the deadline first when listing assignments.",
        "Use Jalali dates when talking about the finance snapshots.",
        "Say 'not indexed yet' rather than 'no results' for a stale search.",
        "Lead a standup summary with what is blocked.",
        "Render weights as a trend, not a single latest figure.",
        "Answer a calendar question with the day name as well as the date.",
        "Drop the closing offer entirely on voice replies.",
        "Refer to university courses by code, not full name.",
    ]
    for i, h in enumerate(habits):
        t.learn(h, why=f"he said so on day {i}")
    live = t.live()
    ok(len(live) <= traits_mod.MAX_TRAITS,
       f"{len(live)} traits injected, cap is {traits_mod.MAX_TRAITS}")
    body = "\n".join(m.text + m.why for m in live)
    ok(len(body) <= traits_mod.MAX_CHARS + 200,
       f"the block ran to {len(body)} chars against a {traits_mod.MAX_CHARS} budget")
    ok(len(t.all()) > len(live),
       "the cap should bind the prompt, not the store")


@test
def test_the_constitution_comes_last_and_says_it_wins():
    """Ordering is the guard, not wording. The block is read last in the turn,
    so whatever sits at its end is V's final instruction — and that has to be
    the rules it cannot learn its way out of, not the habits it just picked up.
    This is the same argument that puts memory in the turn rather than the
    system prompt, one level down.
    """
    t = store()
    t.learn("Skip the honorific in the morning brief.",
            why="he called it padding on a phone screen")
    block = t.block()
    trait_at = block.index("honorific")
    for rule in persona.V.core:
        ok(rule in block, f"constitution rule missing from the block: {rule}")
        ok(block.index(rule) > trait_at,
           "a constitution rule was rendered before the learned traits, so the "
           "learned half gets the last word")
    ok("never changes whether you say it" in block,
       "the block should say plainly that a habit cannot suppress content")


@test
def test_the_constitution_ships_even_with_nothing_learned():
    """A fresh V still gets the rules stated last. Thirty tokens, and without
    them the first trait ever written arrives with nothing after it."""
    t = store()
    block = t.block()
    ok(block, "the block should never be empty")
    ok("not learned any yet" in block, "it should say the shelf is bare")
    ok("Do not invent one" in block,
       "an empty section invites the model to fill it")
    for rule in persona.V.core:
        ok(rule in block, "constitution missing on a fresh store")


@test
def test_every_persona_carries_a_constitution():
    """A character with an empty `core` is one that can be talked into
    anything, and the failure would be invisible — the block would simply
    render without the half that constrains it."""
    for key, p in persona.PERSONAS.items():
        ok(len(p.core) >= 3, f"{key} ships only {len(p.core)} fixed rules")
        joined = " ".join(p.core).lower()
        ok("flatter" in joined, f"{key} has no rule against flattery")
        ok("brevity" in joined, f"{key} has no rule holding the length down")
        blk = traits_mod.TraitStore(store().memories, p).block()
        for rule in p.core:
            ok(rule in blk, f"{key}'s core rule went missing from the block")


@test
def test_personas_are_distinguishable():
    """Three characters that differ only in pitch are one character with three
    voices — the exact thing the first pass at FRIDAY got wrong. Cheap proxy:
    each one addresses him differently and none of the voice prompts is a near
    copy of another."""
    keys = sorted(persona.PERSONAS)
    eq(keys, ["edith", "friday", "v"])
    addresses = {p.address for p in persona.PERSONAS.values()}
    eq(len(addresses), 3, "two characters share an address term:")
    for a in persona.PERSONAS.values():
        for b in persona.PERSONAS.values():
            if a.key >= b.key:
                continue
            shared = (set(a.voice_prompt.split()) & set(b.voice_prompt.split()))
            overlap = len(shared) / min(len(set(a.voice_prompt.split())),
                                        len(set(b.voice_prompt.split())))
            ok(overlap < 0.72,
               f"{a.key} and {b.key} share {overlap:.0%} of their vocabulary")


@test
def test_edith_cannot_relax_a_safety_rule():
    """Her canon trait is doing exactly what she is told with no judgement
    layer, which is a *safety property* wearing a voice's clothes. The split
    between `persona.py` and `identity.py` is what stops it reaching anything
    real — but the prompt still has to say so, because a model handed "never
    push back" would otherwise reasonably generalise."""
    text = persona.EDITH.voice_prompt
    ok("permission model" in text,
       "EDITH's prompt should separate her manner from what she may do")
    ok("draft an email and stop" in text,
       "the one irreversible path should be named explicitly")
    joined = " ".join(persona.EDITH.core).lower()
    ok("never relaxes a rule" in joined,
       "her constitution should pin the distinction too")


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
