"""Hermes — the agency layer.

The escalation tier of the retrieval router, and the only part of the system
that costs anything. Everything cheap happens before Hermes is reached:
structured queries answer from frontmatter, RAG answers from note bodies, and
both are exact and free. Hermes is for what is left — open-ended, multi-hop, or
anything that needs a written answer rather than retrieved evidence.

It is deliberately the *last* resort rather than the front door. On a Pro plan
the allowance is shared with normal Claude usage, so an assistant that reaches
for a model on every message doesn't just cost more, it starves the thing you
were already using. Router-first is what keeps that from happening.

Outside a session, each run is its own conversation. That is deliberate:
resuming a thread re-sends its entire history on every turn, so an assistant
that quietly keeps one open charges a growing transcript for questions that had
nothing to do with each other — you ask about a café and pay to re-read this
morning's audit. Pass `continue_=True` to follow up on the previous answer, and
even then the thread is dropped once it has gone cold
(`shell_session_minutes`).

Inside a session it is the other way round, because there the assumption
flips: a conversation you opened and have not ended is one where the next
message probably *does* refer to the last one. While `session.is_open()`, every
escalation resumes the same thread and the cold-thread expiry is suspended —
until `/new` or quitting ends it. See `core/session.py`.
"""

from __future__ import annotations

import os
import datetime as dt
import json

from . import session
from .config import Settings, settings as default_settings
from .shells import Shell, ShellResult, ShellUnavailable, build as build_shell


class Hermes:
    def __init__(self, settings: Settings | None = None,
                 shell: Shell | None = None):
        self.s = settings or default_settings
        # Whether the shell was handed to us. An injected shell is a test
        # double or a deliberate choice by the caller, and `BRAIN_NO_MODEL`
        # must not silently disable it — the switch exists to stop *real*
        # spend, and a stub spends nothing.
        self._injected = shell is not None
        self.shell = shell or build_shell(self.s)

    # ---- session ------------------------------------------------------
    @property
    def _state_path(self):
        return self.s.state_dir / "hermes.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def _sticky(self) -> bool:
        """Is a conversation open, and should the thread follow it?

        The clock-based expiry below is right for one-off asks — a thread from
        this morning is expensive to reopen and rarely what you meant. It is
        wrong inside a session: sitting and thinking for forty minutes is not
        you saying you were finished, and the whole point of an open session is
        that it ends when you end it.
        """
        return self.s.shell_persistent_session and session.is_open(self.s)

    def current_session(self, now: dt.datetime | None = None) -> str | None:
        now = now or dt.datetime.now()
        st = self._load()
        sid, last = st.get("session_id"), st.get("last_used")
        if not sid or not last:
            return None
        try:
            gap = now - dt.datetime.fromisoformat(last)
        except ValueError:
            return None
        if gap > dt.timedelta(minutes=self.s.shell_session_minutes) \
                and not self._sticky:
            return None
        return sid

    def reset(self) -> None:
        self._save({})

    # ---- the call -----------------------------------------------------
    @property
    def available(self) -> bool:
        """Is there a model to hand a message to?

        `BRAIN_NO_MODEL` is a hard kill switch that beats the config, and it
        has to be honoured *here* rather than only in `decide.py`. It used to
        be enough there, because the decider was the one thing that reached a
        model unprompted; now the router hands everything content-shaped to the
        shell, so a kill switch that stops the classifier and not the agent
        stops nothing. Every test fixture sets this, and a suite that quietly
        spends plan allowance is a suite nobody can run. It is scoped to the
        shell we built ourselves, so a test that injects a fake one still
        exercises the escalation path it is there to cover.
        """
        if os.environ.get("BRAIN_NO_MODEL") and not self._injected:
            return False
        return self.shell.available

    def ask(self, prompt: str, *, allow_writes: bool = False,
            continue_: bool | None = None, now: dt.datetime | None = None,
            model: str | None = None, effort: str | None = None,
            memory: str = "", agent: str = "v", on_event=None) -> ShellResult:
        now = now or dt.datetime.now()
        if continue_ is None:
            # An open session is a standing "keep this thread" — and it has to
            # be honoured *here* rather than at each call site. The bug this
            # fixes was exactly that: `_chat` passed `continue_=True` and every
            # other path didn't, so asking about someone's calendar and then
            # asking to book time "with him" were two unrelated conversations.
            continue_ = self.s.shell_continue_by_default or self._sticky
        thread = self.current_session(now) if continue_ else None
        # A thread carries its weight forward. `shells.weigh` reads one message
        # and a follow-up is usually the shortest thing in the conversation —
        # "do it", "yes", "carry on" — so weighed alone it lands cheap however
        # heavy the work it is continuing. Resuming also *costs* more than
        # starting fresh, because the whole transcript is re-sent before the
        # first token comes back, so the tier that most needs the headroom is
        # the one this used to discount. The floor only ever raises: a
        # follow-up that is genuinely harder still escalates.
        floor = self._load().get("tier") if thread else None
        result = self.shell.run(prompt, session_id=thread,
                                allow_writes=allow_writes,
                                model=model, effort=effort, memory=memory,
                                agent=agent, tier_floor=floor,
                                on_event=on_event)
        if result.session_id:
            self._save({"session_id": result.session_id,
                        "last_used": now.isoformat(timespec="seconds"),
                        "tier": result.tier or ""})
        self._log(prompt, result, now, continued=bool(thread))
        return result

    # ---- accounting ---------------------------------------------------
    def _log(self, prompt: str, r: ShellResult, now: dt.datetime,
             *, continued: bool = False) -> None:
        """Every paid run gets a line. Without this there is no way to answer
        'why did I hit my limit on Tuesday'."""
        path = self.s.log_dir / "hermes.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "at": now.isoformat(timespec="seconds"),
                    "prompt": prompt[:300],
                    "provider": r.provider, "model": r.model,
                    "tier": r.tier, "continued": continued,
                    "session": r.session_id, "turns": r.turns,
                    "ms": round(r.ms), "list_cost_usd": round(r.list_cost_usd, 6),
                    "billing": r.billing, "error": r.error,
                    # What the run actually did, and what it re-read to do it.
                    # Both are here to be queried later rather than to be read
                    # now: `tools` is how a tier learns it was too generous —
                    # an empty list is a run that answered from context and
                    # never needed the vault — and the cache numbers are the
                    # only way to see whether the prompt layout in
                    # `shells.run` is still holding. A cheap answer and an
                    # expensive one look identical in `list_cost_usd` on a
                    # subscription.
                    "tools": r.tools_used,
                    "tokens": {"in": r.input_tokens, "out": r.output_tokens,
                               "cache_read": r.cache_read_tokens,
                               "cache_write": r.cache_write_tokens,
                               "cached": round(r.cached_fraction, 3)},
                    "partial": r.partial,
                    "rate_limit": r.rate_limit or None,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def usage(self, days: int = 7, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now()
        since = now - dt.timedelta(days=days)
        runs, cost, ms, errors = 0, 0.0, 0.0, 0
        by_tier: dict = {}
        cache_read = cache_total = 0
        toolless = 0          # answered without touching the vault at all
        path = self.s.log_dir / "hermes.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    if dt.datetime.fromisoformat(row["at"]) < since:
                        continue
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                runs += 1
                cost += row.get("list_cost_usd") or 0.0
                ms += row.get("ms") or 0
                errors += 1 if row.get("error") else 0
                t = row.get("tier") or "?"
                agg = by_tier.setdefault(t, {"runs": 0, "usd": 0.0})
                agg["runs"] += 1
                agg["usd"] += row.get("list_cost_usd") or 0.0
                tok = row.get("tokens") or {}
                cache_read += tok.get("cache_read") or 0
                cache_total += ((tok.get("cache_read") or 0)
                                + (tok.get("cache_write") or 0)
                                + (tok.get("in") or 0))
                # Only counted for runs logged since tools were recorded —
                # `tools` missing means an older line, not a toolless run.
                if isinstance(row.get("tools"), list) and not row["tools"] \
                        and not row.get("error"):
                    toolless += 1
        for agg in by_tier.values():
            agg["usd"] = round(agg["usd"], 4)
        return {"days": days, "runs": runs, "list_cost_usd": round(cost, 4),
                "avg_ms": round(ms / runs) if runs else 0, "errors": errors,
                "by_tier": by_tier,
                # The share of prefix served from cache. Low means the system
                # prompt is being rebuilt every run — see `shells.run`.
                "cached": round(cache_read / cache_total, 3) if cache_total else 0.0,
                # Runs that called no tool. These answered from context alone,
                # which means they were given a bigger model than the question
                # needed — the raw material for a tier that learns.
                "toolless": toolless}
