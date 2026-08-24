"""The half of V's character that grows.

`persona.py` is what V ships with: a frozen block of prose per character,
snapshotted into `identity.SYSTEM` at import so the system prompt stays a
byte-identical cacheable prefix. That is the right shape for a *constitution*
and the wrong shape for everything else, because it means the only way V's
manner can ever change is Parsa editing a Python file.

This module is the other half. A **trait** is one learned habit of speech —
"drop the honorific in the morning brief, he reads it on his phone" — carried
with the evidence that produced it, injected after the voice block, reinforced
when it holds and decayed when nothing touches it.

Three decisions worth defending:

**It is a memory, not a new store.** Traits are rows in the `memories` table
with `kind='voice'`. Everything a growing personality needs — supersession so
two contradictory habits cannot both be live, `why` so a later contradiction is
judged against the original reason, decay so the character reflects the last
few months rather than everything ever, an event log, a Markdown mirror Parsa
can read in Obsidian — is already built and tested there. A parallel store
would be four hundred lines of the same thing, drifting.

**The constitution cannot be amended.** Each persona names a handful of rules
that are not up for learning (see `Persona.core`): the tone stays level, bad
news still goes first, brevity still wins. They are rendered *after* the
learned traits and said to outrank them, on the same argument that puts memory
in the turn rather than the system prompt — the last word wins. This is the
guard against the failure mode that actually kills adaptive personalities: a
character that learns from one person converges on agreeing with him. A trait
may change how V says a thing. It may never change whether V says it.

**No pattern decides what a trait is.** `memory.py` documents at length how a
regex on the incoming message once stored a request as a standing instruction;
the same trap is open here and wider, because "he seemed happier with that
reply" is not something a rule can read. So the write is the model's, through
`vault_voice_learn`, and the one mechanical bar is that `why` must be non-empty
— a trait with no evidence behind it is refused. That is checkable without
reading intent, which is exactly why it is the check.

What is deliberately *not* here: nothing writes a trait on its own yet. The
nightly reflection job that reads the day's sessions and proposes at most one
change is the obvious next step and is a separate piece of work; until then
traits arrive the way memories do, from Parsa correcting V in conversation.
"""

from __future__ import annotations

import time

from . import memory as memory_mod
from . import persona as persona_mod

# Two ceilings, both small on purpose.
#
# The block is appended to every escalation, so it is priced like the voice
# prompt it amends — and the voice prompt is already a third of the system
# prompt. Eight lines is roughly 150 tokens: enough for a character to be
# recognisably shaped by a year of use, not enough for it to outweigh the
# constitution it sits above. When the cap binds, the weakest trait falls out
# of the prompt rather than out of the store, exactly like `MIN_INJECT_STRENGTH`
# in `memory.py` — V stops *acting* on a habit long before it forgets it.
MAX_TRAITS = 8
MAX_CHARS = 700


class TraitStore:
    """The voice layer over a `MemoryStore`. Owns no SQL of its own."""

    def __init__(self, memories: memory_mod.MemoryStore,
                 persona: persona_mod.Persona | None = None):
        self.memories = memories
        self.persona = persona or persona_mod.active(memories.s)

    # ---- writing -----------------------------------------------------
    def learn(self, text: str, why: str, *, source: str = "told",
              now: float | None = None) -> memory_mod.Memory:
        """Record a habit of speech. `why` is not optional.

        Everything else — deduplication against a near-identical habit,
        retiring the one it contradicts, the event log — is the memory store's,
        because a trait competes with other traits on exactly the terms a
        memory competes with other memories of its kind.
        """
        if not (why or "").strip():
            raise ValueError(
                "a trait needs its evidence — what Parsa said or did that "
                "justifies it. Without one there is no way to judge it later, "
                "and nothing stops you inventing a flattering habit.")
        return self.memories.remember(
            text, kind=memory_mod.VOICE, why=why, source=source, now=now)

    def forget(self, trait_id: int, reason: str = "asked to") -> bool:
        m = self.memories.get(trait_id)
        if not m or m.kind != memory_mod.VOICE:
            return False
        return self.memories.forget(trait_id, reason=reason)

    def reinforce(self, trait_id: int, detail: str = "") -> None:
        self.memories.reinforce(trait_id, detail)

    # ---- reading -----------------------------------------------------
    def all(self, *, active_only: bool = True) -> list[memory_mod.Memory]:
        return self.memories.all(active_only=active_only,
                                 kind=memory_mod.VOICE)

    def live(self, now: float | None = None) -> list[memory_mod.Memory]:
        """The traits strong enough to be worth prompt space, strongest first.

        Unlike `memory.recall`, relevance to the question is not consulted.
        A habit of speech governs every reply and shares no keywords with any
        of them — the same reason `PREFERENCE` is in `ALWAYS_ON` next door.
        """
        now = now or time.time()
        rows = [m for m in self.all()
                if m.effective_strength(now) >= memory_mod.MIN_INJECT_STRENGTH]
        rows.sort(key=lambda m: -m.effective_strength(now))
        out: list[memory_mod.Memory] = []
        budget = MAX_CHARS
        for m in rows[:MAX_TRAITS]:
            cost = len(m.text) + len(m.why) + 12
            if out and cost > budget:
                break
            budget -= cost
            out.append(m)
        return out

    def block(self, now: float | None = None) -> str:
        """The section appended to a run's turn, or "" when there is nothing.

        Returns the constitution even with no traits learned. That is not
        redundancy with the system prompt: `SYSTEM` is read first and this is
        read last, and the whole point of the arrangement is that the rules
        which cannot be amended are the final word rather than the opening one.
        Costs about thirty tokens to say so.
        """
        rows = self.live(now)
        lines = ["## Voice — what you have learned"]
        if rows:
            lines.append(
                "These amend the Voice section of your system prompt. Each one "
                "came from Parsa; the note after it is the evidence. Follow "
                "them over the shipped wording where they disagree.")
            lines.append("")
            for m in rows:
                lines.append(f"- {m.text}  _(#{m.id}, because: {m.why})_")
            lines.append("")
        else:
            lines.append(
                "You have not learned any yet. Do not invent one to fill the "
                "space — write a trait only when Parsa has actually told you "
                "something about how you sound.")
            lines.append("")
        lines.append("These do not move, whatever you have learned above:")
        lines += [f"- {rule}" for rule in self.persona.core]
        lines.append(
            "\nA learned habit changes *how* you say something. It never "
            "changes whether you say it, and it never softens what he would "
            "rather not hear. If one of the traits above is wrong or stale, "
            "drop it with `vault_voice_forget` and tell him you did.")
        return "\n".join(lines)

    # ---- introspection -----------------------------------------------
    def describe(self) -> list[str]:
        """One line per live trait, for `v persona`."""
        now = time.time()
        cutoff = {m.id for m in self.live(now)}
        out = []
        for m in self.all():
            strength = m.effective_strength(now)
            state = ("holding" if m.id in cutoff
                     else "fading" if strength >= memory_mod.MIN_INJECT_STRENGTH
                     else "below the line")
            out.append(f"[{m.id}] {m.text}\n    why: {m.why}\n    "
                       f"strength {strength:.2f} · used {m.used_count}× · "
                       f"{m.age_days(now):.0f}d old · {state}")
        return out


def build(engine) -> TraitStore:
    """The store for a live engine, wearing the engine's persona."""
    return TraitStore(engine.memory, getattr(engine, "persona", None))
