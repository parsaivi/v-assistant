"""The engine. One entry point: `Engine.handle(text) -> Response`.

This is the thing a Telegram bot will later be a thin shell around. Nothing in
here knows what Telegram is — it takes a message and returns a structured
response with optional buttons. Swap the transport, keep the brain.

Cost model: every path here is free except one. Structured queries, RAG,
captures, reminders and nudges are SQL and numpy — `Response.cost` is 0.0 and
the tests assert it. The single exception is escalation to Hermes, reached only
when no frontmatter schema matched, and only when a shell has been deliberately
turned on. On a subscription even that is not billed per run, so `cost` stays
0.0 and the allowance consumed travels in `data["list_cost_usd"]`.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from . import (agenda as agenda_mod, brag as brag_mod, calendar as calendar_mod,
               capture, deferred as deferred_mod, hermes as hermes_mod,
               identity, index,
               journal as journal_mod, llm, memory as memory_mod, notes, nudges,
               persona, query, rag, reminders, router, schema,
               session as session_mod,
               tasks as tasks_mod, traits as traits_mod, work)
from .config import Settings, settings as default_settings
from .vaultfs import Vault, VaultError


@dataclass
class Button:
    """A choice the user can make. Telegram renders these as inline buttons."""

    label: str
    action: str
    payload: dict = field(default_factory=dict)


@dataclass
class Response:
    text: str
    intent: str = ""
    citations: list[str] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    cost: float = 0.0
    ms: float = 0.0
    route: str = ""      # how it was handled: structured:places, rag:tfidf, …
    decision: str = ""   # what the router decided, with its confidence

    def render(self) -> str:
        out = self.text
        if self.citations:
            out += "\n\nSources: " + " ".join(self.citations[:6])
        if self.buttons:
            out += "\n\n[" + "] [".join(b.label for b in self.buttons) + "]"
        return out


class Engine:
    def __init__(self, settings: Settings | None = None,
                 surface: str = persona.CLI):
        self.s = settings or default_settings
        # Where this process's messages come from. One front-end per process,
        # so it is set once here rather than passed on every `handle()`. It is
        # what decides *which agent answers* — see `_agent_for`.
        self.surface = surface
        self.vault = Vault(self.s)
        # Before the first write, not after. V commits per-path (`git commit --
        # <rel>`), so appending to a note the user had already edited by hand
        # would sweep their edit into a `[bot]` commit — and `undo` would then
        # revert both halves. It also leaves the tree clean, which is what
        # `undo`'s `git revert` needs in order to run at all.
        self.startup_commit = None
        if self.s.git_commit_on_start:
            try:
                self.startup_commit = self.vault.commit_pending()
            except VaultError:
                pass
        self.conn = index.connect(self.s)
        # Refresh before anything reads the mirror. The old build answered from
        # whatever was last indexed, so a note written this morning was
        # invisible and a note deleted last week still counted — and it said
        # neither. The check is one stat() per note.
        self.index_refreshed = None
        if self.s.auto_index:
            try:
                self.index_refreshed = index.refresh_if_stale(self.s, self.conn)
            except Exception:
                pass
        # Which character V is wearing. The free paths below say their lines
        # from here rather than hardcoding them, so a persona switch reaches
        # the greetings and the sign-offs too and not only the model's prompt.
        # Which agent is answering on this surface. Not simply "what the
        # config says": an agent that may not be reached from here does not
        # get to answer from here either. EDITH is configured and a Telegram
        # message arrives — V takes it, and `agent_denied` is what the switch
        # handler uses to say so out loud rather than silently changing voice.
        self.persona, self.agent_denied = self._agent_for(self.surface)
        self.q = query.QueryEngine(self.s, self.conn)
        self.rag = rag.Rag(self.s, self.conn)
        self.capture = capture.Capture(self.vault, self.s, self.rag)
        self.reminders = reminders.Reminders(self.s, self.conn)
        self.nudges = nudges.NudgeEngine(self.s, self.q)
        self.work = work.Work(self.s, self.vault)
        self.brag = brag_mod.BragWriter(self.s, self.q, self.vault)
        self.llm = llm.build(self.s)
        self.hermes = hermes_mod.Hermes(self.s)
        # The router hands everything that isn't a command to the model — but
        # only if there is a model to hand it to. With no shell, or escalation
        # switched off, it falls back to the rule cascade instead of routing
        # every message to a handler that would answer "no model is available".
        self.router = router.Router(
            self.s,
            agentic=bool(self.s.shell_auto_escalate and self.hermes.available))
        self.agenda = agenda_mod.Agenda(self.s)
        self.memory = memory_mod.MemoryStore(
            self.s, self.conn,
            # Lazy: building the index is minutes on a cold cache, and a
            # greeting must not pay for it. `encoder()` returns None until one
            # exists, and memory falls back to lexical overlap.
            embedder=_Lazy(self.rag.encoder),
            # Each agent keeps its own beliefs; EDITH additionally reads the
            # others'. See `persona.Persona.memory_scope` for why the read is
            # one-way.
            scope=self.persona.scope, reads=self.persona.reads_scopes)
        # The voice layer over the same store: rows of kind 'voice', rendered
        # into the turn as an amendment to the persona's shipped wording. See
        # `core/traits.py` for why it is a view rather than a second table.
        self.traits = traits_mod.TraitStore(self.memory, self.persona)
        self.journal = journal_mod.Journal(self.s, self.vault)
        # Questions the plan window or an expired login refused, waiting for
        # the time that fixes them. See `core/deferred.py`.
        self.deferred = deferred_mod.DeferredQueue(self.s)
        # Opened by the REPL, not here: constructing an Engine happens for
        # `send`, the MCP server and the scheduler too, and none of those is a
        # conversation with an end. `record()` no-ops until one is started.
        self.sessions = session_mod.SessionStore(self.s)
        # What V last said, so a bare "no, that's wrong" has something to be
        # a correction *of* when it gets stored.
        self._last_said: str = ""
        # The last thing filed, so "no, that was a lyric" has something to
        # move. This replaced the ask-before-writing prompt: the correction
        # happens after the write instead of gating it, which is free because
        # every write is a revertible commit.
        self._last_capture: dict | None = None
        # Where progress goes while a run is in flight, or None for silence.
        # An attribute rather than an argument threaded through `handle`,
        # because it is a property of the *surface* — the window shows tool
        # arms, Telegram edits a message, `send` shows nothing — and not of any
        # one question. `shells.ShellEvent` says what arrives.
        self.progress = None

    # -----------------------------------------------------------------
    # --- which agent is speaking ------------------------------------------
    def _agent_for(self, surface: str) -> tuple[persona.Persona, str]:
        """The agent that may answer on this surface, and what was refused.

        The refusal is returned rather than raised because there is always an
        answer: V is reachable everywhere and is what everything falls back
        to. A silent fallback would be the worst outcome — Parsa would be
        talking to V while believing he had EDITH, which is precisely the
        confusion the surface gate exists to prevent.
        """
        want = persona.active(self.s)
        if want.allows(surface):
            return want, ""
        return persona.get(persona.DEFAULT), want.key

    def _summons_persist(self, target: persona.Persona) -> bool:
        """Does summoning `target` here outlive the session?

        Two conditions, and both are about not writing a preference nobody
        expressed. An agent that is not reachable everywhere may not be the
        stored default anywhere: `edith` in the config would leave the next
        Telegram message resolving to an agent it may not use, and falling
        back on every single message — she is summoned for the session and
        lapses when it ends, which is the honest reading of "only accessible
        through the TUI".

        And a summons only persists from the terminal. Elsewhere the name is a
        wake word, said fresh on every utterance, so storing it buys nothing —
        and it silently reached surfaces that were never asked: one "Friday"
        to the phone rewrote `state/config.json`, and the TUI came up as
        Friday for days afterwards. `v persona --use` is still the way to
        change the default deliberately, from anywhere.
        """
        return not target.surfaces and self.surface in persona.PERSISTS_FROM

    def switch_agent(self, key: str) -> tuple[bool, str]:
        """Summon an agent by name. Returns (switched, message).

        Whether the switch is written to the config is `_summons_persist`;
        either way it holds for the rest of this session.
        """
        target = persona.get(key)
        if target.key != persona.ALIASES.get(key.strip().lower(), key.strip().lower()):
            return False, f"I don't know an agent called {key!r}."
        if target.key == self.persona.key:
            return False, ""
        if not target.allows(self.surface):
            where = ", ".join(target.surfaces)
            return False, (f"{target.key.upper()} is reachable from the "
                           f"{where} only. You are on {self.surface}.")
        self.persona = target
        # Each agent has its own beliefs, so the stores are rebuilt rather
        # than re-pointed — `scope` is read in the constructor and half a
        # dozen queries depend on it.
        self.memory = memory_mod.MemoryStore(
            self.s, self.conn, embedder=_Lazy(self.rag.encoder),
            scope=target.scope, reads=target.reads_scopes)
        self.traits = traits_mod.TraitStore(self.memory, target)
        # The shell keeps a thread, and a thread carries the previous agent's
        # transcript. Without this the new agent reads the old one's replies
        # and imitates them — measured: switching to FRIDAY mid-session and
        # watching her say "sir" for a full turn.
        try:
            self.hermes.reset()
        except Exception:
            pass
        if not self._summons_persist(target):
            return True, ""      # session-only; nothing written to config
        try:
            path = self.s.state_dir / "config.json"
            raw = json.loads(path.read_text()) if path.exists() else {}
            raw["persona"] = target.key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except Exception:
            pass                 # the switch still holds for this session
        return True, ""

    def handle(self, text: str, *, message_type: str = "text",
               has_location: bool = False, has_file: bool = False,
               extra: dict | None = None, now: dt.datetime | None = None
               ) -> Response:
        t0 = time.time()
        now = now or dt.datetime.now()
        extra = extra or {}
        text = (text or "").strip()

        route = self.router.route(text, message_type=message_type,
                                  has_location=has_location, has_file=has_file)
        resp = self._dispatch(route, text, now, extra,
                              voice=message_type == "voice")
        resp.intent = route.intent
        # `route` is how the message was *handled* — "structured:places",
        # "rag:tfidf", "capture.idea". The router's own decision goes in
        # `decision`. Overwriting one with the other threw away the only
        # signal that says whether an answer cost anything.
        resp.decision = str(route)
        if not resp.route:
            resp.route = str(route)
        resp.ms = (time.time() - t0) * 1000
        # Remembered so the next message can be a correction *of this*. Without
        # it, "no, that's wrong" gets stored as a memory with no referent.
        self._last_said = resp.text
        self._log(text, route, resp)
        # Both halves of the exchange, per turn, on disk. Per turn rather than
        # at the end because the transcript's job is to survive the run that
        # produced it — including the ones that die without an exit.
        self.sessions.record(text, resp, now)
        return resp

    # ---- the conversation --------------------------------------------
    def start_session(self, now: dt.datetime | None = None) -> list:
        """Begin a conversation. Returns recaps of any left open by a previous
        run — flushed first, so they land in their own day's log rather than
        being folded into this one.

        Dropping the thread here is the other half of `end_session`'s, and it
        is the half that holds when nothing was ended cleanly. `hermes._sticky`
        suspends the cold-thread expiry for as long as *a* session is open —
        not for as long as the session that opened the thread is open — so any
        thread still named in `state/hermes.json` when a session starts is
        adopted by it, however old and whoever created it. Measured: a Telegram
        message at 20:28 opened thread `39abbf9e`; no session was open, so
        nothing ever ended and nothing reset it; `v repl` seventeen hours later
        resumed that thread and grew it to 240K tokens, at which point the
        persona had washed out of the far end of the context and V answered in
        the CLI's own voice, from beliefs formed on a different surface the
        previous day.

        With this, a thread can only ever be extended past the expiry by the
        session it was created in, which is what `_sticky` was always meant to
        say. The cost is one cold prefix per session — the same cost `/new`
        already pays, and much less than re-reading a day-old transcript on
        every turn.
        """
        filed = []
        for orphan in self.sessions.orphans(now):
            self.journal.session(orphan)
            filed.append(orphan)
        self.hermes.reset()
        self.sessions.start(now)
        return filed

    def end_session(self, reason: str = "quit",
                    now: dt.datetime | None = None):
        """End it, write it down, and drop the thread.

        Resetting Hermes is the half that makes "then it should be a new
        thread" true: without it the next run would resume this conversation's
        history, which is both wrong and billed. Returns the recap, or None if
        nothing was said.
        """
        recap = self.sessions.close(reason, now)
        if recap:
            recap.filed = self.journal.session(recap)
        self.hermes.reset()
        return recap

    # -----------------------------------------------------------------
    def _dispatch(self, route: router.Route, text: str, now: dt.datetime,
                  extra: dict, voice: bool = False) -> Response:
        if route.intent == router.SUMMON:
            return self._summon(route)
        if route.intent == router.SMALLTALK:
            return self._smalltalk(route)
        if route.intent == router.TEACH:
            return self._teach(route, text)
        if route.intent == router.RECALL:
            return self._recall()
        if route.intent == router.HELP:
            return self._help()
        if route.intent == router.STATUS:
            return self.status()
        if route.intent == router.UNDO:
            return self.undo()
        if route.intent == router.BRIEF:
            return self.brief(now)
        if route.intent == router.NUDGE_CHECK:
            return self.nudge_digest()
        if route.intent == router.REMINDER:
            return self._reminder(text, now)
        if route.intent == router.TASKS:
            return self._tasks(route, now)
        if route.intent == router.TASK_DONE:
            return self._task_done(route, text, now)
        if route.intent == router.TASK_ADD:
            return self._task_add(route, now)
        if route.intent == router.CALENDAR:
            return self._calendar(route, now)
        if route.intent == router.SCHEDULE:
            return self._schedule(route, text, now)
        if route.intent in (router.AGENT, router.COMPOUND):
            return self._compound(text, now, voice=voice)
        if route.intent == router.CHAT:
            return self._chat(text, now, voice=voice)
        if route.intent == router.REFILE:
            return self._refile(route, now)
        if route.intent == router.QUERY:
            return self.ask(text, voice=voice)
        if route.intent in router.CAPTURE_INTENTS:
            return self._capture(route, text, now, extra)
        return self.ask(text, voice=voice)

    # ---- smalltalk (free, and that is the whole point) ----------------
    def _summon(self, route: router.Route) -> Response:
        """Someone said an agent's name. Free — no model, no vault read.

        The reply comes from the agent that *ends up* speaking, not the one
        that was called, so a refused summons is answered in the voice Parsa
        is actually still talking to. Hearing EDITH's flat register tell you
        EDITH is unavailable would be its own small lie.
        """
        name = route.slots.get("name", "")
        switched, refusal = self.switch_agent(name)
        say = functools.partial(self.persona.line, name=identity.NAME)
        if refusal:
            return Response(refusal, route="summon:denied",
                            data={"agent": self.persona.key, "wanted": name})
        if not switched:
            # Already wearing it. Answer as a greeting rather than as a no-op:
            # "edith" typed twice is someone checking she is still there.
            return Response(say("greeting", when="Still here", tail=""),
                            route="summon:noop",
                            data={"agent": self.persona.key})
        p = self.persona
        bits = [f"{p.key.upper()} online, {p.address}."]
        n = len(self.memory.all(own_only=True))
        bits.append(f"{n} memor{'y' if n == 1 else 'ies'} of my own"
                    + (f", reading {' and '.join(p.reads_scopes)}'s."
                       if p.reads_scopes else "."))
        if p.model:
            bits.append(f"{p.model} · {p.effort or 'medium'} effort.")
        if p.writes == persona.DIRECT:
            bits.append("Vault writes land without confirmation; "
                        "anything leaving the vault still does not.")
        return Response(" ".join(bits), route=f"summon:{p.key}",
                        data={"agent": p.key, "surface": self.surface,
                              "session_only": not self._summons_persist(p)})

    def _smalltalk(self, route: router.Route) -> Response:
        """Answer a greeting without a model.

        Every one of these used to cost real allowance: "how are you" scored as
        a question, matched no schema, and went straight to the agent. Nothing
        here needs reasoning — it needs a sentence, and a sentence that
        actually says what V is good for beats a warm one.
        """
        flavour = route.slots.get("flavour", "greeting")
        hour = dt.datetime.now().hour
        say = functools.partial(self.persona.line, name=identity.NAME)
        if flavour == "greeting":
            when = ("Good morning" if 5 <= hour < 12 else
                    "Good afternoon" if 12 <= hour < 18 else "Good evening")
            pend = len(self.reminders.pending())
            text = say("greeting", when=when,
                       tail=(f"{pend} reminder{'s' if pend != 1 else ''} pending."
                             if pend else "Nothing outstanding."))
        elif flavour == "thanks":
            text = say("thanks")
        elif flavour == "how":
            st = index.stats(self.s)
            pend = len(self.reminders.pending())
            text = say("how", notes=st["notes"], pending=pend,
                       plural="s" if pend != 1 else "")
        elif flavour == "who":
            n = self.memory.stats()["active"]
            text = say("who", memories=n, plural="s" if n != 1 else "")
        else:
            text = say("night")
        return Response(text, route=f"smalltalk:{flavour}")

    # ---- learning from Parsa ------------------------------------------
    def _teach(self, route: router.Route, text: str) -> Response:
        """`/forget N`. That is the entire local half of the memory loop now.

        Writing used to happen here, off a regex that decided a message was a
        correction because it started with "no". It produced exactly two
        memories, both of them one-off requests stored verbatim as standing
        instructions. Deciding *what is worth remembering* is a judgement about
        meaning, so it belongs where the other judgements about meaning now
        live: the model writes through `vault_memory_write` and repairs its own
        entries with `vault_memory_revise`. A number after `/forget` is the only
        part of this a pattern can get right.
        """
        forget_id = route.slots.get("forget")
        if forget_id is None:
            return self._compound(text, now=dt.datetime.now())
        m = self.memory.get(forget_id)
        if not m:
            return Response(f"No memory #{forget_id}.", route="memory:miss")
        self.memory.forget(forget_id)
        self._sync_memory_note()
        return Response(f"Forgotten: “{m.text}”", route="memory:forget")

    def _recall(self) -> Response:
        rows = self.memory.all()
        if not rows:
            return Response(
                "Nothing yet. I write these myself, when something you tell "
                "me or correct me on looks like it will still be true next "
                "month — the lesson, not the sentence you said.\n\n"
                "`/forget <n>` drops one if I've kept something I shouldn't.",
                route="memory:empty")
        st = self.memory.stats()
        n = st["active"]
        lines = [f"**{n} thing{'s' if n != 1 else ''} I remember**", ""]
        for kind, label in ((memory_mod.PREFERENCE, "How you want me to work"),
                            (memory_mod.CORRECTION, "Corrections"),
                            (memory_mod.FACT, "Facts"),
                            (memory_mod.PATTERN, "Patterns")):
            block = [m for m in rows if m.kind == kind]
            if not block:
                continue
            lines.append(f"_{label}_")
            for m in block:
                fade = ("  ·  _fading_"
                        if m.effective_strength() < memory_mod.MIN_INJECT_STRENGTH
                        else "")
                lines.append(f"· `#{m.id}` {m.text}{fade}")
            lines.append("")
        lines.append("`/forget <n>` to drop one. Full detail, including what I "
                     "no longer believe: [[19 - Assistant/V/Memory]]")
        return Response("\n".join(lines), route="memory:list",
                        data={"count": st["active"]})

    def _sync_memory_note(self) -> None:
        try:
            self.journal.write_memory(self.memory.export_markdown())
        except Exception:
            pass

    # ---- capture -----------------------------------------------------
    def _capture(self, route: router.Route, text: str, now: dt.datetime,
                 extra: dict, confirmed: bool = False) -> Response:
        result = self.capture.capture(text, route, when=now, extra=extra,
                                      confirmed=confirmed)
        if result.change:
            self._remember_capture({"text": text, "extra": extra,
                                    "intent": result.intent,
                                    "commit": result.change.commit,
                                    "target": result.target,
                                    "at": now.isoformat(timespec="seconds")})

        msg = result.message
        if result.duplicates:
            rel, score = result.duplicates[0]
            msg += (f"\n\nYou've written something close to this before "
                    f"({score:.0%} similar): [[{rel[:-3]}]]")
        buttons = [Button("↩ Undo", "undo", {"commit": result.change.commit})] \
            if result.change else []
        return Response(msg, citations=[f"[[{result.target[:-3]}]]"] if result.target
                        else [], buttons=buttons,
                        # The router's own confidence, carried out to the
                        # surface. Below the threshold this is what makes the
                        # answer *look* unsure — the file still happens either
                        # way, and it is still one sentence to move it.
                        data={"target": result.target,
                              "confidence": route.confidence,
                              "commit": result.change.commit if result.change else None})

    # ---- what was filed last, and how to move it ----------------------
    # On disk, not just in memory: `cli send` is a new process every time, so an
    # in-memory pointer would make "no, that was a lyric" work in the REPL and
    # silently do nothing everywhere else.
    @property
    def _capture_path(self):
        return self.s.state_dir / "last_capture.json"

    def _remember_capture(self, entry: dict) -> None:
        self._last_capture = entry
        try:
            self._capture_path.write_text(json.dumps(entry), encoding="utf-8")
        except OSError:
            pass

    def _recall_capture(self, now: dt.datetime) -> dict | None:
        entry = self._last_capture
        if entry is None:
            try:
                entry = json.loads(self._capture_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        # An hour is the honest window. "That was a lyric" tomorrow morning
        # means some other note, and reverting the wrong commit is worse than
        # saying there's nothing to move.
        try:
            at = dt.datetime.fromisoformat(entry.get("at", ""))
        except ValueError:
            return None
        return entry if now - at <= dt.timedelta(hours=1) else None

    def _forget_capture(self) -> None:
        self._last_capture = None
        self._capture_path.unlink(missing_ok=True)

    def _refile(self, route: router.Route, now: dt.datetime) -> Response:
        """"No, that was a lyric." Revert the last capture and redo it.

        This is the whole correction loop, moved from before the write to after
        it. The old flow asked first and learned from the button press; asking
        first is what made V unable to hold a conversation, and the write was
        always revertible anyway, so nothing is lost by guessing and fixing.
        """
        last = self._recall_capture(now)
        intent = route.slots.get("intent", "")
        if not last:
            return Response("Nothing recent to move.", route="refile:none")
        if intent == last["intent"]:
            return Response(
                f"That's where it already is — [[{last['target'][:-3]}]].",
                route="refile:noop")
        stranded = ""
        if last["commit"]:
            try:
                self.vault.undo(last["commit"])
            except Exception as exc:
                return Response(f"Couldn't take the first one back: {exc}",
                                route="refile:failed")
        else:
            # No git, so nothing to revert — the first copy stays put, and
            # saying so beats leaving a duplicate the user never learns about.
            stranded = (f"\n\n(The first copy is still in "
                        f"[[{last['target'][:-3]}]] — git is off, so I "
                        f"couldn't take it back.)")
        self._forget_capture()
        self.router.learn(last["text"], intent)   # the self-improvement loop
        resp = self._capture(router.Route(intent, 1.0, "you told me so"),
                             last["text"], now, last["extra"], confirmed=True)
        resp.text += ("\n\n(Noted — I'll route messages like that the same "
                      "way.)" + stranded)
        resp.route = f"refile:{intent}"
        return resp

    def _chat(self, text: str, now: dt.datetime, voice: bool = False) -> Response:
        """Talking to V rather than at the vault.

        The one path that is conversation, not retrieval — "tell me about
        yourself", "what do you think of this", "talk to me". It escalates,
        because a canned string cannot answer any of them, and it continues the
        session so the second message knows about the first. Everything cheap
        has already been tried: the router only gets here when no rule matched.
        """
        escalated, failure = self._escalate(
            text, evidence=self.rag.search(text), continue_=True, voice=voice)
        if escalated:
            return escalated
        # No model — say what V *can* do rather than apologise in the abstract.
        resp = self._help()
        head = failure or (f"No model is configured, so I can't hold a real "
                           f"conversation — `shell_provider` is "
                           f"`{self.s.shell_provider}`. What I can do without "
                           f"one:")
        resp.text = f"{head}\n\n{resp.text}"
        resp.route = "chat:unserved"
        return resp

    # ---- retrieval ---------------------------------------------------
    def ask(self, question: str, voice: bool = False) -> Response:
        """Three-way retrieval router: structured → semantic → (agentic, paid)."""
        structured = self._structured(question)
        if structured:
            return structured

        # Retrieve first, then escalate — and hand the retrieval *to* the model.
        #
        # The earlier version escalated blind, on the reasoning that the agent
        # has `vault_search` and can look things up itself. It can; it doesn't
        # always. Asked "what was that youtube clip about being an expert?" it
        # answered in one turn, without a single tool call, that there was no
        # record of a YouTube clip in the vault — while
        # `09 - Anti-Library/04 - YouTube/Veritasium - The Expert Myth.md` sat
        # there as the top keyword hit. Telling a model to search is a request;
        # putting the passages in its prompt is a fact. Search costs ~250ms and
        # nothing, so it now runs every time and serves two purposes: grounding
        # when the agent works, and the answer itself when it doesn't.
        res = self.rag.search(question)
        escalated, failure = self._escalate(question, evidence=res, voice=voice)
        if escalated:
            return escalated

        if not res.hits:
            return Response(
                (failure + "\n\n" if failure else "")
                + "Nothing in the vault matches that."
                + ("" if self.rag.ready else
                   " (The semantic index isn't built yet — run `index --embed`.)"),
                data={"degraded": bool(failure)}, route="rag:empty")

        lines = []
        # A dead or rate-limited agent is not a reason to answer nothing. The
        # free path still works, and retrieved evidence beats an apology — this
        # used to return "The agent failed: you've hit your session limit" and
        # stop, discarding a search that would have taken 40ms and cost zero.
        if failure:
            lines += [failure, ""]
        lines += [f"Closest matches in the vault (semantic, {res.backend}):", ""]
        for h in res.hits[:5]:
            where = f"{h.rel[:-3]}" + (f" › {h.heading}" if h.heading else "")
            lines.append(f"• **{where}**\n  {h.preview(200)}")
        lines.append("")
        lines.append("_No model is configured, so this is the retrieved evidence "
                     "rather than a written answer._")
        return Response("\n".join(lines), citations=res.citations(),
                        # `degraded` is what turns V's ✦ grey and what puts the
                        # "could not be reached" step into `/why`. Set here
                        # rather than inferred from the route, because the same
                        # route is also the healthy no-model-configured answer.
                        data={"hits": len(res.hits), "backend": res.backend,
                              "degraded": bool(failure)},
                        route=f"rag:{res.backend}")

    def _escalate(self, question: str, *, evidence=None,
                  allow_writes: bool = False,
                  continue_: bool | None = None,
                  voice: bool = False) -> tuple[Response | None, str]:
        """Hand it to the model. Returns (answer, why-it-didn't-work).

        The second half of that tuple is the point: a failed escalation must
        degrade to the free path rather than replace it. Auth expiry and
        session limits are the two failures that actually happen, and both used
        to end the turn with an apology while a working local search sat
        unused.

        Returns (None, "") when no shell is configured — the default — so the
        zero-cost posture holds until one is deliberately turned on.
        """
        if not self.s.shell_auto_escalate:
            return None, ""
        try:
            if not self.hermes.available:
                return None, ""
            # Memory rides along on every escalation. This is the only place it
            # can matter: the free paths are SQL and have no opinion to steer,
            # so a preference like "call them MRs" is either in the model's
            # prompt or it may as well not have been learned.
            context = self.memory.context_block(question)
            # The character's learned half, in the turn for the same reason
            # memory is: `SYSTEM` carries the persona's shipped voice block and
            # has to stay byte-identical to keep its cache, so an amendment
            # that changes as V learns cannot live up there. Read last, so it
            # wins where it disagrees — and it carries the constitution after
            # it, so *that* wins in turn. See `core/traits.py`.
            # `identity.SYSTEM` snapshots one persona's voice at import so it
            # stays a cacheable prefix, which means an agent summoned *since*
            # start-up would otherwise be answered in the previous one's voice
            # — the exact failure measured when Friday said "sir" for a full
            # turn. Sending the live agent's block in the turn fixes it without
            # touching the prefix: the common case (no switch) sends nothing
            # extra and stays byte-identical.
            if self.persona.key != identity.PERSONA_KEY:
                context = (context + "\n\n" if context else "") + (
                    f"You are {self.persona.key.upper()} for this "
                    "conversation, not the character described in the system "
                    "prompt. The block below replaces that one entirely.\n\n"
                    + self.persona.voice_prompt)
            voice_block = self.traits.block()
            if voice_block:
                context = (context + "\n\n" if context else "") + voice_block
            if self.persona.edits:
                # The system prompt tells every agent that writes are
                # append-only and that nothing may touch existing prose. For
                # this one agent that is no longer true, and the correction
                # has to arrive here rather than up there: `SYSTEM` is shared
                # and byte-identical by design, and it is read *first*, so a
                # contradiction resolved in the turn resolves the right way.
                context = (context + "\n\n" if context else "") + (
                    "Editing: you have `vault_edit`, `vault_set_meta` and "
                    "`vault_rewrite`, which the system prompt above says do "
                    "not exist. They do, for you, because you only ever run "
                    "at Parsa's own terminal. That paragraph is otherwise "
                    "still correct — it is describing every other agent.\n"
                    "Read a note with `vault_read` in this same turn before "
                    "you edit it; never edit from what you remember of it "
                    "earlier in the conversation. Prefer `vault_set_meta` for "
                    "a frontmatter field, `vault_edit` for a passage, and "
                    "`vault_rewrite` only when the note is genuinely being "
                    "restructured. Say which notes you changed and what the "
                    "undo command is.")
            if self.persona.writes == persona.DIRECT:
                context = (context + "\n\n" if context else "") + (
                    "Vault writes: make them, then report what you wrote. Do "
                    "not show a preview and wait — every vault write is "
                    "append-only, committed per path, and reverts with "
                    "`vault_undo`. This applies to the vault ONLY. Anything "
                    "that leaves it — `gmail_send` above all — keeps every "
                    "confirmation step in the system prompt, because none of "
                    "it is revertible.")
            grounding = _evidence_block(evidence, self.s)
            if grounding:
                context = (context + "\n\n" if context else "") + grounding
            # The system prompt caps a reply at one or two sentences, which is
            # right for almost everything and wrong for "explain how you did
            # that". Saying so in the turn rather than swapping the system
            # prompt is deliberate: `SYSTEM` has to stay byte-identical between
            # runs or it stops being a cacheable prefix (see `shells.py`), and
            # the turn is read last anyway, so it wins.
            if router.wants_detail(question):
                context = (context + "\n\n" if context else "") + (
                    "Parsa asked for the long answer here, so the one-or-two"
                    "-sentence ceiling does not apply to this reply. Answer at "
                    "whatever length the question actually needs — but answer "
                    "the question he asked, not a summary of your own process.")
            if voice:
                # Without this, "can you hear me" reads as an honest question
                # about live audio and gets an honest, technically-correct
                # answer that sounds like the feature is broken: "I have no
                # audio input" — true of the model, but not what someone who
                # just watched their voice memo turn into this exact text
                # wants to hear. The words below are a transcript, not a
                # denial that anything arrived.
                context = (context + "\n\n" if context else "") + (
                    "This message came in as a voice memo — the text above "
                    "and below is a transcript (faster-whisper), not typed "
                    "text. If asked whether you can hear him or received the "
                    "audio, say plainly that you read a transcript of what "
                    "he said rather than perceiving sound directly — don't "
                    "imply nothing arrived.")
            r = self.hermes.ask(question, memory=context,
                                allow_writes=allow_writes,
                                continue_=continue_,
                                # A floor, not a preference: an agent reached
                                # deliberately and rarely should not be tiered
                                # down to haiku for a question that merely
                                # looks cheap. Empty for V and Friday, so the
                                # router's tiering is untouched for them.
                                model=self.persona.model or None,
                                effort=self.persona.effort or None,
                                # …so the MCP subprocess opens its memory at
                                # this agent's scope, not the default one.
                                agent=self.persona.scope,
                                on_event=self.progress)
        except Exception as exc:                 # a dead shell must not 500
            return None, _shell_excuse(str(exc))
        # A run that was cut off having already written something is not a
        # failure to be swallowed. Half an answer plus a line saying it is half
        # is worth more than the local-search fallback, which knows less and
        # took none of the four minutes that were just spent.
        if not r.ok and r.partial and r.text.strip():
            return Response(
                r.text.rstrip() + "\n\n_" + _shell_excuse(r.error or "").strip("_")
                + " This is as far as it got._",
                ms=r.ms,
                data={"provider": r.provider, "model": r.model, "tier": r.tier,
                      "turns": r.turns, "session": r.session_id,
                      "tools": r.tools_used, "partial": True, "degraded": True,
                      "list_cost_usd": r.list_cost_usd, "billing": r.billing},
                route=f"agent:{r.provider}:partial"), ""
        if not r.ok:
            excuse = _shell_excuse(r.error or "unknown error")
            # A window that is full and a login that has expired both end
            # today's attempt without ending the question. Park it, and say
            # when it will be answered rather than only why it wasn't — the
            # difference between an apology and an appointment.
            parked = self.deferred.park(question, error=r.error or "",
                                        rate_limit=r.rate_limit, voice=voice)
            if parked and parked.reason == deferred_mod.LIMIT:
                excuse = (excuse.rstrip("_ ")
                          + f" I'll ask again at {parked.when()} and bring you "
                            "the answer then._")
            elif parked:
                # An expired login does not fix itself, and saying "I'll ask
                # again at 23:40" about one would be a promise resting on
                # Parsa doing something he hasn't been told is the condition.
                excuse = (excuse.rstrip("_ ")
                          + " I've kept the question — log back in and I'll "
                            f"answer it (I'll also retry at {parked.when()} "
                            "in case you already have)._")
            return None, excuse
        return Response(r.text, ms=r.ms,
                        cost=r.list_cost_usd if r.billing == "api-key" else 0.0,
                        data={"provider": r.provider, "model": r.model,
                              "tier": r.tier,
                              "turns": r.turns, "session": r.session_id,
                              "list_cost_usd": r.list_cost_usd,
                              "billing": r.billing,
                              "tools": r.tools_used,
                              "cached": round(r.cached_fraction, 3),
                              "rate_limit": r.rate_limit or None},
                        route=f"agent:{r.provider}"), ""

    def _structured(self, question: str) -> Response | None:
        """Questions with a schema get a SELECT, not a search. Exact and free."""
        low = question.lower()

        def has(*words: str) -> bool:
            return any(w in low for w in words)

        # Naming a note is the most direct question there is; answer it before
        # any keyword matching gets a chance to read "ISSUE-139" as a topic.
        if router.NOTE_REF.match(question.strip()):
            res = self.q.by_identifier(question)
            if res.rows or "No " in res.answer:
                return self._as_response(res, "note")

        if has("in-review", "in review") and has("issue"):
            return self._as_response(self.q.issues(status="in-review"), "issues in review")
        if has("stale", "stuck") and has("review"):
            return self._as_response(self.q.stale_reviews(), "stale reviews")
        if has("blocked", "waiting on"):
            return self._as_response(self.q.blocked(), "blocked work")
        if has("issue") and has("open"):
            return self._as_response(self.q.issues(status="open"), "open issues")
        if has("merge request", " mr", "mrs"):
            status = "open" if "open" in low else ("merged" if "merged" in low else None)
            return self._as_response(self.q.merge_requests(status=status), "merge requests")
        if has("workout", "gym", "lift", "bench", "squat", "deadlift"):
            if has("last", "latest"):
                return self._as_response(self.q.last_workout(), "last workout")
            return self._as_response(self.q.workouts(), "workouts")
        if has("cafe", "café", "coffee", "place", "restaurant", "work from",
               "somewhere", "to work", "to sit", "to study", "work at"):
            return self._as_response(
                self.q.places(
                    category="cafe" if has("cafe", "café", "coffee") else None,
                    wifi=True if "wifi" in low else None,
                    outdoor=True if "outdoor" in low else None,
                    open_late=True if has("late", "open late") else None,
                    min_rating=8 if has("best", "good", "top") else None,
                ), "places")
        if has("birthday"):
            return self._as_response(self.q.birthdays(30), "birthdays")
        if has("haven't talked", "havent talked", "haven't i talked",
               "havent i talked", "overdue", "lost touch", "should i call",
               "out of touch", "neglecting"):
            return self._as_response(self.q.overdue_contacts(), "overdue contacts")
        if has("assignment", "due"):
            return self._as_response(self.q.assignments(), "assignments")
        if has("orphan"):
            return self._as_response(self.q.orphans(), "orphan notes")
        if has("broken link"):
            return self._as_response(self.q.broken_links(), "broken links")
        if has("goal", "objective"):
            return self._as_response(self.q.goals(), "goals")
        if has("on this day", "a year ago", "last year"):
            return self._as_response(self.q.on_this_day(), "on this day")
        if has("standup", "stand-up"):
            return Response(self.work.standup(), route="structured:standup")

        # Questions about V itself. These used to escalate — "how many notes do
        # I have" spent five agent turns and 20 seconds calling vault_status,
        # which is a SELECT. Anything V can answer about its own state, it
        # should answer from its own state.
        if has("how many note", "how many file", "notes do i have",
               "size of my vault", "how big is my vault"):
            st = index.stats(self.s)
            return Response(
                f"**{st['notes']} notes** — {st['words']:,} words, "
                f"{st['chunks']} chunks indexed, {st['links']} links.",
                route="structured:vault size")
        if has("what do you remember", "what have you learned",
               "what do you know about me"):
            return self._recall()
        if has("how much have you cost", "how much did you cost",
               "what have you spent", "your cost", "allowance"):
            u = self.hermes.usage(7)
            return Response(
                f"**≈${u['list_cost_usd']} of plan allowance over "
                f"{u['runs']} run(s) in the last 7 days** "
                f"(avg {u['avg_ms']}ms, {u['errors']} error(s)). "
                f"Everything else I did was free.",
                route="structured:usage")
        return None

    def _as_response(self, result: query.Result, label: str) -> Response:
        body = f"**{result.answer}**"
        if result.rows:
            body += "\n\n```\n" + result.table(12) + "\n```"
        return Response(body, citations=result.citations()[:6],
                        data={"kind": result.kind, "count": len(result.rows)},
                        route=f"structured:{label}")

    # ---- tasks (free: the checkbox layer) ----------------------------
    def _day_of(self, when: str, now: dt.datetime) -> dt.date:
        today = now.date()
        return {"tomorrow": today + dt.timedelta(days=1),
                "yesterday": today - dt.timedelta(days=1)}.get(when, today)

    def _tasks(self, route: router.Route, now: dt.datetime) -> Response:
        view = route.slots.get("view", "open")
        day = self._day_of(route.slots.get("when", ""), now)

        if view == "backlog":
            res = self.q.backlog(day)
            body = [f"**{res.answer}**", "", "```", res.table(12), "```"]
            worst = self.q.tasks.overdue(day)[:3]
            if worst:
                body += ["", "Oldest still open:"]
                body += [f"· {t.render(today=day)}" for t in worst]
            return Response("\n".join(body), route="structured:backlog",
                            data={"kind": "backlog"})

        if view == "overdue":
            return self._as_response(self.q.overdue_tasks(day), "overdue")
        if view == "priorities":
            res = self.q.priorities(day)
            if not res.rows:
                return Response("Nothing open — no unfinished tasks in the "
                                "vault.", route="structured:priorities")
            lines = [f"**{res.answer}**", ""]
            for i, r in enumerate(res.rows, 1):
                t = r.extra["_task"]
                lines.append(f"{i}. {t.render(today=day)}")
                lines.append(f"   ↳ {t.link()}")
            evs = self.q.calendar.on(day)
            if evs:
                lines += ["", f"Around {len(evs)} event(s) that day:"]
                lines += [f"· {e.render()}" for e in evs]
            return Response("\n".join(lines), route="structured:priorities",
                            citations=list(dict.fromkeys(
                                r.extra["_task"].link() for r in res.rows)),
                            data={"kind": "priorities", "count": len(res.rows)})
        source = route.slots.get("source", "")
        res = self.q.open_tasks(folder=source)
        if source and not res.rows:
            return Response(
                f"Nothing open under `{source}/`. "
                f"({len(self.q.tasks.open())} open tasks elsewhere — say "
                "`tasks` for all of them.)", route="structured:tasks")
        return self._as_response(res, "tasks")

    def _task_done(self, route: router.Route, text: str,
                   now: dt.datetime) -> Response:
        target = route.slots.get("target", "").strip()
        matches = self.q.find_task(target)
        if not matches:
            close = self.q.find_task(target, open_only=False)
            if close and not close[0][0].is_open:
                t = close[0][0]
                return Response(
                    f"“{t.text}” is already ticked in {t.link()}.",
                    route="structured:task-done")
            return Response(
                f"I can't find an open task matching “{target}”. "
                f"There are {len(self.q.tasks.open())} open tasks — "
                "say `tasks` to see them, or tell me which note it's in.",
                route="structured:task-missing")

        best, score = matches[0]
        runner = matches[1][1] if len(matches) > 1 else 0.0
        # Ticking the wrong box edits a note the user hand-wrote, so an
        # ambiguous match asks rather than picking. The margin matters more
        # than the score: two tasks at 0.7 is a coin flip, one at 0.7 is not.
        if score - runner < 0.15 and len(matches) > 1:
            lines = [f"Which one? “{target}” matches {len(matches)}:", ""]
            lines += [f"{i}. {t.render()}" for i, (t, _) in
                      enumerate(matches[:4], 1)]
            return Response(
                "\n".join(lines),
                buttons=[Button(t.text[:28], "complete_task", {"id": t.id})
                         for t, _ in matches[:4]],
                route="structured:task-ambiguous")

        return self._complete(best, now)

    def _task_add(self, route: router.Route, now: dt.datetime) -> Response:
        text = route.slots.get("text", "").strip()
        when = route.slots.get("when", "")
        due = self._day_of(when, now) if when else None
        if due:
            # The date is captured in `📅 2026-08-02`; leaving "tomorrow" in the
            # text too means the task still says "tomorrow" next week.
            text = re.sub(rf"\s*\b{re.escape(when)}\b\s*$", "", text,
                          flags=re.I).strip(" .,")
        # The daily note is the default home: that is where this vault's tasks
        # already live, and a task filed somewhere the user doesn't look is a
        # task that doesn't exist.
        rel = self._today_note_rel(now.date())
        heading = "Tasks"
        if not self.vault.exists(rel):
            heading = None
        try:
            line = self.q.tasks.add(text, self.vault, rel=rel, heading=heading,
                                    due=due)
        except Exception as exc:
            return Response(f"Couldn't add that: {exc}", route="task-add:error")
        # `tasks.add` re-indexes the note it wrote to, so the new row is
        # already queryable. This used to rebuild the whole vault to find one
        # checkbox, which took seconds and hid the fact that the MCP path —
        # which has no rebuild — never picked it up at all.
        where = f"[[{rel[:-3]}]]"
        extra = f" due {due.isoformat()}" if due else ""
        return Response(f"Added to {where}: `{line.strip()}`{extra}",
                        citations=[where], buttons=[Button("Undo", "undo", {})],
                        route="structured:task-add")

    @staticmethod
    def _today_note_rel(d: dt.date) -> str:
        return (f"01 - Daily Notes/{d.year}/{d.strftime('%m - %B')}/"
                f"{d.isoformat()}.md")

    def _complete(self, task, now: dt.datetime) -> Response:
        try:
            self.q.tasks.complete(task, self.vault, today=now.date())
        except (ValueError, OSError) as exc:
            return Response(f"Couldn't tick that: {exc}",
                            route="structured:task-error")
        left = len(self.q.tasks.open())
        return Response(
            f"Ticked **{task.text}** in {task.link()}. {left} open left.",
            citations=[task.link()],
            buttons=[Button("Undo", "undo", {})],
            data={"task_id": task.id}, route="structured:task-done")

    # ---- calendar (free: the vault is the source of truth) -----------
    def _calendar(self, route: router.Route, now: dt.datetime) -> Response:
        when = route.slots.get("when", "")
        if route.slots.get("view") == "next":
            # "When is my next meeting" wants a time and a gap, not a table of
            # today. Answering with a day's grid makes the reader do the
            # arithmetic the question was asking for.
            ev = self.q.calendar.next_up(now)
            if not ev:
                return Response("Nothing scheduled in the next two weeks.",
                                route="structured:calendar-next")
            at = ev.starts_at()
            gap = ""
            if at:
                mins = int((at - now).total_seconds() // 60)
                if 0 <= mins < 60:
                    gap = f" — in {mins} min"
                elif 0 <= mins < 60 * 24:
                    gap = f" — in {mins // 60}h {mins % 60}m"
            day = ("today" if ev.date == now.date()
                   else "tomorrow" if ev.date == now.date() + dt.timedelta(1)
                   else ev.date.strftime("%a %d %b"))
            return Response(f"**{ev.summary}** — {day} at {ev.time_label}{gap}"
                            + (f" _({ev.location})_" if ev.location else ""),
                            route="structured:calendar-next")
        if when in ("this week", "next week"):
            start = now.date() + dt.timedelta(days=7 if when == "next week" else 0)
            res = self.q.events(start, days=7)
        else:
            res = self.q.events(self._day_of(when, now))
        body = [f"**{res.answer}**"]
        if res.rows:
            body += ["", "```", res.table(20), "```"]
        else:
            # An empty calendar and an unconfigured one are different answers,
            # and conflating them is what produced "I don't have access to an
            # external calendar" for a question about the user's own notes.
            if not (self.s.vault / calendar_mod.FOLDER).exists():
                body.append(
                    "\nNo calendar file exists yet. Tell me an event — "
                    "\"on Aug 8 at 12:30 I have a meeting\" — and I'll start "
                    f"`{calendar_mod.FOLDER}/`.")
            else:
                body.append(f"\nThe calendar is `{calendar_mod.FOLDER}/` if "
                            "you want to add something.")
        return Response("\n".join(body), route="structured:calendar",
                        data={"kind": "calendar", "count": len(res.rows)})

    def _schedule(self, route: router.Route, text: str,
                  now: dt.datetime) -> Response:
        ev = calendar_mod.parse_natural(text, today=now.date())
        # The date and time stay with the parser — those must be exactly right
        # and a regex is better at them than a model. The *title* is the part
        # the parser can only approximate, because it works by deletion: strip
        # the times and dates and whatever words remain are the summary. On
        # "for today calander set my meeting on 13:00 and not 17:30" what
        # remained was "calander set meeting and not". If the decider read the
        # message, take its title instead; it read for meaning, not leftovers.
        title = str((route.slots or {}).get("title") or "").strip()
        if ev and title:
            ev = dataclasses.replace(ev, summary=title)
        if not ev:
            return Response(
                "I got that it's an appointment but couldn't pin the date and "
                "time. Try \"on Aug 8 at 12:30 — meeting with Sajad\".",
                route="structured:schedule-unclear")
        said = self.q.calendar.add(ev)
        already = said.startswith("already")
        synced = self.q.calendar.sync_daily_note(ev.date)
        if already:
            return Response(
                f"Already there: **{ev.date.strftime('%a %d %b')} "
                f"{ev.time_label} — {ev.summary}**. Nothing written twice.",
                route="structured:schedule-dup")
        # A calendar entry the user asked to be *reminded* about also needs a
        # reminder — the note is the record, the reminder is the interruption,
        # and the scenario that motivated this asked for both in one sentence.
        lines = [f"On the calendar: **{ev.date.strftime('%a %d %b')} "
                 f"{ev.time_label} — {ev.summary}**"]
        rem = None
        if re.search(r"remind|یادم", text, re.I) or not ev.all_day:
            at = self._remind_at(ev)
            if at > now:
                rem = self.reminders.add(ev.summary, at)
        if rem:
            lines.append(f"I'll remind you at "
                         f"{rem.due.strftime('%a %d %b %H:%M')}.")
        where = f"[[{calendar_mod.FOLDER}/{ev.date.strftime('%Y-%m')}]]"
        if synced:
            lines.append(f"Written to {where} and rendered into that day's "
                         "note.")
        else:
            lines.append(f"Written to {where}. That day's note doesn't exist "
                         "yet — it'll pick this up when it's created.")
        return Response("\n".join(lines), route="structured:schedule",
                        buttons=[Button("Undo", "undo", {})],
                        data={"date": ev.date.isoformat()})

    @staticmethod
    def _remind_at(ev) -> dt.datetime:
        """15 minutes before a timed event; 08:00 for an all-day one."""
        if ev.all_day:
            return dt.datetime.combine(ev.date, dt.time(8, 0))
        return ev.starts_at() - dt.timedelta(minutes=15)

    # ---- compound requests -------------------------------------------
    def _compound(self, text: str, now: dt.datetime, voice: bool = False) -> Response:
        """Several asks in one message: hand it to the agent, pre-loaded.

        The failure this exists to prevent is silent and cheap: "based on my
        1on1, update my goals and create a todo list" matched the noun "goals",
        printed a table of goal note titles in 0ms for $0, and stopped. Free
        and instant is only a virtue when the answer is right.
        """
        del now  # the state dump this used to print was its only reader
        # Writes are on for this path only. The router reached COMPOUND because
        # the message carries an action verb — "update", "create", "mark" — so
        # the user has asked for a change, and an agent that can only read
        # answers "I don't have write access" to a request to write. Everything
        # it can reach is still append-only, `[bot]`-committed and revertible;
        # `WRITE_TOOLS` has no general file writer in it.
        escalated, failure = self._escalate(
            text, evidence=self.rag.search(text), allow_writes=True, voice=voice)
        if escalated:
            return escalated
        # Nothing but the reason. This used to print `_working_state` — the
        # backlog shape, the top five tasks, the next seven days — on the
        # theory that state was better than nothing. It is not: the question
        # was "do the job I told you", and twenty lines of tasks answer a
        # question nobody asked while burying the one line that matters. It
        # also read as though the failure had been handled.
        return Response(
            failure or "That needs more than a lookup — several things at "
                       "once — and no model is available to do it.",
            route="compound:unserved")

    def _working_state(self, now: dt.datetime) -> str:
        s = self.q.tasks.backlog_shape(now.date())
        top = self.q.tasks.priorities(today=now.date(), limit=5)
        evs = self.q.calendar.between(now.date(),
                                      now.date() + dt.timedelta(days=7))
        out = [f"Open tasks: {s['open']} ({s['overdue']} past their date, "
               f"{s['rotting']} untouched >2 months)."]
        if top:
            out.append("Top of the list right now:")
            out += [f"  · {t.render(today=now.date())}" for t in top]
        if evs:
            out.append("Next 7 days:")
            out += [f"  · {e.date.isoformat()} {e.render()}" for e in evs[:6]]
        return "\n".join(out)

    # ---- reminders ---------------------------------------------------
    def _reminder(self, text: str, now: dt.datetime) -> Response:
        rem = self.reminders.add_natural(text, now=now)
        if not rem:
            return Response(
                "I couldn't find a time in that. Try \"in 20 minutes\", "
                "\"tomorrow at 9\", or \"every day at 7\".")
        when = rem.due.strftime("%a %d %b at %H:%M")
        rec = f", repeating every {rem.recurrence}" if rem.recurrence else ""
        return Response(f"Reminder set for {when}{rec} — {rem.text}",
                        buttons=[Button("Cancel", "cancel_reminder", {"id": rem.id})],
                        data={"reminder_id": rem.id}, route="reminder")

    def due_reminders(self, now: dt.datetime | None = None) -> list[Response]:
        out = []
        for rem in self.reminders.due_now(now):
            late = rem.overdue_by
            suffix = ""
            if late > dt.timedelta(minutes=5):
                mins = int(late.total_seconds() // 60)
                suffix = f" _(late by {mins} min — I was down)_"
            out.append(Response(f"⏰ {rem.text}{suffix}",
                                buttons=[Button("Snooze 30m", "snooze",
                                                {"id": rem.id, "minutes": 30})],
                                data={"reminder_id": rem.id}, route="reminder:fire"))
            self.reminders.mark_fired(rem, now=now)
        return out

    # ---- questions the window refused --------------------------------
    def answer_deferred(self, now: dt.datetime | None = None) -> list[Response]:
        """Ask again whatever the plan window turned away, now that it can.

        Called by the scheduler, so this runs in a process with nobody
        watching — which is why the answer carries the question with it. An
        answer to something asked four hours ago, arriving on its own, is
        unreadable without it.

        Re-entering `handle` rather than escalating directly is deliberate: it
        is the same message it was, and it should be routed the same way. Only
        escalations are ever parked, so nothing here can re-run a capture and
        write a second copy of a note.
        """
        now = now or dt.datetime.now()
        out = []
        for item in self.deferred.due(now):
            resp = self.handle(item.question, now=now,
                               message_type="voice" if item.voice else "text")
            if resp.data.get("degraded"):
                # Still refused. `_escalate` has already moved the entry to
                # the next window on its way through — this only has to notice
                # when it stopped doing so, which is the queue no longer
                # holding the question. Reporting that is the point: three
                # silent failures and a question that evaporates is the
                # behaviour this replaced.
                if not self.deferred.holds(item.question):
                    out.append(Response(
                        f"I could not get an answer to “{item.question}” — "
                        f"{deferred_mod.MAX_TRIES} attempts across several "
                        "windows, and it is still refusing. Ask me again when "
                        "you want it.",
                        route="deferred:gave-up",
                        data={"question": item.question}))
                continue
            self.deferred.drop(item.id)
            resp.text = (f"You asked, when I couldn't answer: "
                         f"“{item.question}”\n\n{resp.text}")
            resp.route = f"deferred:{resp.route}"
            out.append(resp)
        return out

    # ---- proactive ---------------------------------------------------
    def brief(self, now: dt.datetime | None = None) -> Response:
        """The morning brief, assembled deterministically. No prose model needed."""
        now = now or dt.datetime.now()
        today = now.date()
        parts = [f"**{today.strftime('%A %d %B')}**", ""]

        all_nudges = self.nudges.all(today=today)
        urgent = [n for n in all_nudges if n.urgency >= 2]
        rest = [n for n in all_nudges if n.urgency < 2]

        # The vault calendar is the source of truth; the remote feed is only a
        # fallback for days it hasn't been imported into. "Remind me on the
        # morning of the 8th" is answered here — a meeting written to the
        # calendar shows up in that day's brief without anyone setting a
        # separate reminder.
        events = self.q.calendar.on(today)
        if not events:
            try:
                events = self.agenda.events(today)
            except Exception:
                events = []
        if events:
            parts.append("**Today**")
            # `agenda.Event.render()` prefixes its own "· "; the vault's does
            # not. Normalise rather than trusting either.
            parts += [("· " + e.render().lstrip("· ")) for e in events]
            parts.append("")

        top = self.q.tasks.priorities(today=today, limit=5)
        if top:
            parts.append("**Today's shortlist**")
            parts += [f"· {t.render(today=today)}" for t in top]
            parts.append("")

        pending = [r for r in self.reminders.pending()
                   if r.due.date() == today]
        if pending:
            parts.append("**Today's reminders**")
            parts += [f"· {r.due.strftime('%H:%M')} — {r.text}" for r in pending[:5]]
            parts.append("")

        if urgent:
            parts.append("**Needs you**")
            parts += [n.render(with_links=False) for n in urgent]
            parts.append("")

        if rest:
            parts.append("**Worth knowing**")
            parts += [n.render(with_links=False) for n in rest[:5]]
            parts.append("")

        collision = None
        try:
            collision = self.rag.collision()
        except Exception:
            pass
        if collision:
            anchor, old, score = collision
            parts.append(f"**Echo** — you're working on [[{anchor[:-3]}]], and "
                         f"[[{old[:-3]}]] from a while back is close to it "
                         f"({score:.0%}).")

        if len(parts) <= 2:
            parts.append("Nothing needs you. Unusual.")

        # The counts are part of the answer, not decoration: `briefing.signal`
        # weighs them to decide whether this morning is worth a notification,
        # and it must not have to re-derive them by parsing the text back out.
        return Response("\n".join(parts).strip(),
                        data={"nudges": len(all_nudges), "events": len(events),
                              "tasks": len(top), "reminders": len(pending),
                              "urgent": len(urgent)},
                        route="brief")

    def nudge_digest(self) -> Response:
        items = self.nudges.all()
        return Response(nudges.digest(items, max_items=12),
                        data={"count": len(items)}, route="nudges")

    def evening(self, now: dt.datetime | None = None) -> Response:
        """Draft today's daily note from what actually happened. Confirm to write."""
        now = now or dt.datetime.now()
        today = now.date()
        since = dt.datetime.combine(today, dt.time(0, 0))
        commits = self.work.commits_since(since)
        touched = [r["rel"] for r in self.conn.execute(
            "SELECT rel FROM notes WHERE mtime > ? ORDER BY mtime DESC LIMIT 12",
            (since.timestamp(),))]

        lines = [f"Draft for {today}:", ""]
        if commits:
            lines.append("**Commits**")
            lines += [f"- {c}" for c in commits[:8]]
            lines.append("")
        if touched:
            lines.append("**Notes touched**")
            lines += [f"- [[{t[:-3]}]]" for t in touched[:8]]
        if not commits and not touched:
            lines.append("_Nothing recorded today._")

        draft = "\n".join(lines)
        return Response(draft + "\n\nAppend this to today's note?",
                        buttons=[Button("Append", "append_daily",
                                        {"text": draft, "date": today.isoformat()}),
                                 Button("Discard", "discard", {})],
                        route="evening")

    # ---- meta --------------------------------------------------------
    def status(self) -> Response:
        st = index.stats(self.s)
        rg = self.rag.info()
        pend = self.reminders.pending()
        acc = self.router.accuracy_report()
        commits = self.vault.bot_log(3)
        waiting = sorted(self.deferred.all(), key=lambda d: d.retry_after)

        mem = self.memory.stats()

        # The semantic index is a separate artefact from the note index and
        # doesn't refresh on startup (rebuilding embeddings is minutes, not
        # milliseconds). Say so when it has fallen behind, rather than letting
        # search quietly miss every note written since the last build.
        sem = "not built"
        if rg.get("built"):
            sem = (f"{rg['backend']} · {rg['chunks']} chunks · dim {rg['dim']} "
                   f"· {rg['bytes'] / 1e6:.1f} MB")
            behind = st["chunks"] - rg["chunks"]
            if behind > 0:
                sem += (f"  ⚠ {behind} chunks behind — run "
                        f"`index --embed` to make new notes searchable")

        refreshed = ""
        if self.index_refreshed is not None:
            r = self.index_refreshed
            refreshed = f" (auto-refreshed: {r.updated} updated, {r.removed} removed)"

        lines = [
            f"**{identity.NAME} — status**",
            f"· vault: {self.s.vault}",
            f"· notes indexed: {st['notes']} ({st['words']:,} words, "
            f"{st['chunks']} chunks){refreshed}",
            f"· semantic index: {sem}",
            f"· keyword search (FTS5): {'yes' if st['fts'] else 'no'}",
            f"· git: {'on' if self.vault.git_available else 'off'}"
            + (f" · last bot commit: {commits[0]}" if commits else "")
            + (f" · {len(self.vault.pending())} uncommitted"
               if self.vault.pending() else ""),
            # Shown because it is pinned rather than inherited, and because the
            # symptom of getting it wrong (every calendar time off by 4h30m)
            # looks like a calendar bug, not a clock one.
            f"· clock: {dt.datetime.now():%Y-%m-%d %H:%M} "
            f"{self.s.timezone} (UTC{dt.datetime.now().astimezone():%z})",
            f"· reminders pending: {len(pend)}",
            f"· memories: {mem['active']} active"
            + (f", {mem['faded']} fading" if mem["faded"] else "")
            + (f", {mem['superseded_or_forgotten']} superseded"
               if mem["superseded_or_forgotten"] else ""),
            f"· routing corrections learned: {acc['corrections']}",
            f"· reasoning shell: {self.s.shell_provider}"
            + (" (available)" if self.hermes.available else " (unavailable)"),
            f"· speech: faster-whisper "
            f"({'installed' if _stt_ok() else 'not installed'})",
            # Only when there is one. A standing "0 waiting" line teaches you
            # to stop reading the line, which is the one time it will matter.
            *([f"· questions waiting on the plan window: {len(waiting)}"
               f" (next at {waiting[0].when()})"] if waiting else []),
            f"· google: calendar {_google_state('gcal', self.s)}, "
            f"gmail {_google_state('gmail', self.s)}",
        ]
        return Response("\n".join(lines), route="status")

    def undo(self, commit: str | None = None) -> Response:
        try:
            msg = self.vault.undo(commit)
        except Exception as exc:
            return Response(f"Couldn't undo: {exc}", route="undo:failed")
        return Response(f"Done — {msg}", route="undo")

    def validate(self) -> Response:
        report = schema.validate_vault(self.s)
        lines = [f"**{report.summary()}**", ""]
        for issue in report.errors[:15]:
            lines.append(str(issue))
        if len(report.errors) > 15:
            lines.append(f"… {len(report.errors) - 15} more errors")
        return Response("\n".join(lines), data={"errors": len(report.errors)},
                        route="validate")

    def act(self, action: str, payload: dict,
            now: dt.datetime | None = None) -> Response:
        """Handle a button press. The transport calls this, not `handle`."""
        now = now or dt.datetime.now()
        if action == "undo":
            return self.undo(payload.get("commit"))
        if action == "cancel_reminder":
            ok = self.reminders.cancel(int(payload["id"]))
            return Response("Cancelled." if ok else "That one already fired.")
        if action == "snooze":
            r = self.reminders.snooze(int(payload["id"]),
                                      int(payload.get("minutes", 30)))
            return Response(f"Snoozed until {r.due.strftime('%H:%M')}." if r
                            else "Couldn't find that reminder.")
        if action == "append_daily":
            day = dt.date.fromisoformat(payload["date"])
            rel = self.capture.ensure_daily(day)
            change = self.vault.append(rel, payload["text"],
                                       heading=self.s.daily_log_heading,
                                       summary=f"evening draft for {day}")
            return Response(f"Appended to [[{rel[:-3]}]].",
                            buttons=[Button("↩ Undo", "undo",
                                            {"commit": change.commit})])
        if action == "complete_task":
            # Emitted by `_task_done`'s disambiguation buttons since they were
            # written, and by the priorities widget on the home screen — and
            # unhandled here until both had a way to be pressed, which is to
            # say it answered "Unknown action: complete_task" to its own button.
            task = self.q.tasks.get(int(payload["id"]))
            if task is None:
                return Response(
                    "That task isn't in the index any more — the note it was "
                    "in has probably changed. Say `tasks` for the live list.",
                    route="structured:task-missing")
            if not task.is_open:
                return Response(f"“{task.text}” is already ticked.",
                                route="structured:task-done")
            return self._complete(task, now)
        if action == "route":
            forced = router.Route(payload["intent"], 1.0, "chosen")
            return self._capture(forced, payload.get("text", ""), now, {},
                                 confirmed=True)
        if action == "discard":
            return Response("Dropped it.")
        return Response(f"Unknown action: {action}")

    # ---- housekeeping -------------------------------------------------
    def reindex(self, *, embed: bool = False, full: bool = False) -> Response:
        st = index.rebuild(self.s, full=full)
        msg = f"Index rebuilt: {st}"
        if embed:
            info = self.rag.build()
            msg += (f"\nEmbeddings: {info['chunks']} chunks · {info['backend']}"
                    f" · {info['seconds']}s · {info['bytes'] / 1e6:.1f} MB")
        return Response(msg, route="reindex")

    def _help(self) -> Response:
        return Response(
            f"I'm {identity.NAME}. Just say what you want — there are no "
            f"commands to memorise.\n\n"
            "· *\"remind me to call mom tomorrow at 9\"* → a reminder\n"
            "· *\"which issues are in-review?\"* → a free structured query\n"
            "· *\"what did I decide about pricing?\"* → semantic search\n"
            "· *\"hey V, what do you think?\"* → talk to me; that one uses the "
            "model, so it's the only kind that spends anything\n"
            "· *anything else* → captured and filed where it belongs, and "
            "*\"no, that was a lyric\"* moves it if I guessed wrong\n\n"
            "**Teaching me** — this is the part that matters:\n"
            "· *\"always call them MRs, not merge requests\"* → I'll do that "
            "from now on\n"
            "· *\"no, actually …\"* → a correction; it replaces what I believed\n"
            "· *\"remember that …\"* → a fact worth keeping\n"
            "· `/memory` to see everything I've learned, `/forget 4` to drop one\n\n"
            "`/status` `/brief` `/nudges` `/undo` exist for the few things "
            "that aren't sentences. Everything about me is in "
            "[[19 - Assistant/V/README]].", route="help")

    def _log(self, text: str, route: router.Route, resp: Response) -> None:
        """Conversation log — machinery, so it stays out of the vault body."""
        path = self.s.log_dir / f"{dt.date.today()}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "at": dt.datetime.now().isoformat(timespec="seconds"),
                    "text": text[:500], "intent": route.intent,
                    "confidence": route.confidence, "route": resp.route,
                    "decision": resp.decision,
                    "cost": resp.cost, "ms": round(resp.ms, 1),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()


def _evidence_block(res, s: Settings) -> str:
    """The top local hits, phrased for the model's prompt.

    Kept short on purpose — this is a pointer into the vault, not a substitute
    for the model reading the notes. The last line matters most: without it a
    model handed five passages tends to answer *only* from them and stop
    looking, which trades one failure mode for another.

    Private folders are filtered here. This function is an egress point: it
    puts vault text into a model's prompt, and `Rag.search` has no privacy
    filter of its own because local CLI search is allowed to see everything the
    user wrote. The boundary is "never reaches a model", not "never visible",
    so it belongs at the crossing.
    """
    hits = getattr(res, "hits", None)
    if not hits:
        return ""
    hits = [h for h in hits if not s.is_private(h.rel)]
    if not hits:
        return ""
    lines = ["Local search already ran for this question. Top matches in the "
             "vault:"]
    for h in hits[:5]:
        where = h.rel[:-3] if h.rel.endswith(".md") else h.rel
        if getattr(h, "heading", ""):
            where += f" › {h.heading}"
        lines.append(f"- [[{where}]] — {h.preview(180)}")
    lines.append(
        "Read the ones that look relevant before answering, and search further "
        "if none fit. Do not say the vault has no record of something without "
        "having searched for it.")
    return "\n".join(lines)


def _shell_excuse(error: str) -> str:
    """Turn a shell failure into one line that says what to do about it.

    Two of these are routine and neither is a bug: a Claude Code OAuth session
    expires, and a Pro plan has a window limit. Raw text like "Failed to
    authenticate: OAuth session expired and could not be refreshed" tells you
    nothing actionable at the point you're reading it.
    """
    low = error.lower()
    if "session limit" in low or "rate limit" in low or "429" in low:
        note = ("_Plan allowance is used up for now, so this is retrieved "
                "evidence rather than a written answer._")
        when = re.search(r"resets\s+([^\n·]+)", error)
        if when:
            note = note[:-1] + f" Resets {when.group(1).strip()}._"
        return note
    if "authenticate" in low or "oauth" in low or "expired" in low:
        return ("_The Claude Code session has expired — run `claude` once to "
                "log back in. Falling back to local search._")
    if "stalled" in low or "timed out" in low:
        # Not "it took too long" — a run is no longer stopped for being long,
        # only for going silent, so saying "ask something narrower" would be
        # advice against the wrong cause.
        return "_The agent went quiet and was stopped. Nothing was written._"
    return f"_The agent couldn't run ({error.strip()[:120]}), so this is local search._"


class _Lazy:
    """Defers building something expensive until the first `encode` call.

    The memory store wants an embedding backend but almost never uses one —
    only when a new memory has to be compared against existing beliefs. Loading
    an 11 MB index (or, on the sentence-transformers backend, a model) at
    Engine construction would make every greeting pay for it.
    """

    def __init__(self, factory):
        self._factory = factory
        self._obj = None
        self._tried = False

    def encode(self, texts, **kw):
        if not self._tried:
            self._tried = True
            try:
                self._obj = self._factory()
            except Exception:
                self._obj = None
        if self._obj is None:
            raise RuntimeError("no embedding backend available")
        return self._obj.encode(texts, **kw)


def _stt_ok() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _google_state(which: str, s) -> str:
    """`status`'s one-line answer for gcal/gmail — imported lazily so a
    status check doesn't pull in either module (and their http.server-based
    OAuth flow imports) on every engine construction."""
    if which == "gcal":
        from . import gcal as mod
        client = mod.GoogleCalendar(s)
    else:
        from . import gmail as mod
        client = mod.Gmail(s)
    if not client.configured():
        return "not configured"
    if not client.authorised():
        return "configured, not authorised"
    account = client.account()
    return f"connected as {account}" if account else "connected"
