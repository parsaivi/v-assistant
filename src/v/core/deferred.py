"""Questions the model could not take, kept until it can.

Two failures on this machine are not faults and not permanent: the Pro plan's
window fills up, and the Claude Code OAuth session expires. Between them they
account for seven of the thirteen failed runs on record. What used to happen to
those seven is the whole reason this module exists — V degraded to local search,
said one honest line about why, and *dropped the question*. The window reopened
four hours later with nobody to notice, and the thing Parsa actually asked was
never answered by anything.

So a refused question is parked here with the time it can be tried again, and
the scheduler asks it then. The answer arrives unprompted, hours late, marked as
what it is. That is a strictly better outcome than silence, and it is the one
case where V speaking first needs no justification: it is finishing a sentence
Parsa started.

Deliberately *not* a general work queue. Only escalations that failed for a
reason that time fixes go in — never a capture (it was already written), never
a structured query (it already answered), never a model error that would repeat
identically. `tries` is capped, because a question retried forever against a
broken login is a machine talking to itself.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass, asdict, field

from .config import Settings, settings as default_settings

# Why a question was parked. `limit` clears on its own at a known time; `auth`
# needs Parsa to run `claude` once, so it is retried in hope rather than in
# expectation and gives up sooner.
LIMIT, AUTH = "limit", "auth"

# After this many failed attempts V stops and says so. Three is not arithmetic:
# it is one retry for a window that reopened late, one for a login that was
# fixed in between, and a third before admitting that waiting is not the
# problem.
MAX_TRIES = 3


@dataclass
class Deferred:
    question: str
    retry_after: str                  # ISO, naive local — same clock as everything
    reason: str = LIMIT
    asked_at: str = ""
    tries: int = 0
    voice: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def due(self, now: dt.datetime) -> bool:
        try:
            return dt.datetime.fromisoformat(self.retry_after) <= now
        except ValueError:            # an unparseable time is due now, not never
            return True

    def when(self) -> str:
        """The retry time as V would say it out loud."""
        try:
            at = dt.datetime.fromisoformat(self.retry_after)
        except ValueError:
            return "shortly"
        today = dt.datetime.now().date()
        return at.strftime("%H:%M") if at.date() == today \
            else at.strftime("%a %H:%M")


def reason_for(error: str) -> str | None:
    """Which of the two recoverable failures this is, or None for neither.

    None is the important return: a model that answered wrongly, a tool that
    was denied, a prompt that was too long — none of those get better by being
    asked again in four hours, and parking them would turn a bad answer into a
    bad answer delivered twice.
    """
    low = (error or "").lower()
    if "session limit" in low or "rate limit" in low or "429" in low \
            or "usage limit" in low:
        return LIMIT
    if "authenticate" in low or "oauth" in low or "expired" in low:
        return AUTH
    return None


def reset_at(error: str = "", rate_limit: dict | None = None,
             now: dt.datetime | None = None) -> dt.datetime:
    """When to try again.

    Three sources, best first. The stream now carries `resetsAt` as an epoch,
    which is exact and needs no interpretation — that is the one to use when it
    is there. The error text ("resets 3am (Asia/Tehran)") is a fallback for the
    blob path and for older logs, and is read as the *next* time that hour
    comes round rather than today's, or a limit hit at 4am would be retried
    thirteen hours before it lifts. Failing both, an hour from now: short
    enough to be useful, long enough not to spend the retry on a window that
    is still full.
    """
    now = now or dt.datetime.now()
    epoch = (rate_limit or {}).get("resetsAt")
    if epoch:
        try:
            at = dt.datetime.fromtimestamp(float(epoch))
            # Trust it only if it is actually in the future — a stale value
            # from an earlier event in the same run would retry immediately.
            if at > now:
                return at + dt.timedelta(seconds=30)
        except (TypeError, ValueError, OSError):
            pass

    match = re.search(r"resets?\s+(\d{1,2})\s*(am|pm)", error or "", re.I)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(2).lower() == "pm" else 0)
        at = now.replace(hour=hour, minute=1, second=0, microsecond=0)
        if at <= now:
            at += dt.timedelta(days=1)
        return at
    return now + dt.timedelta(hours=1)


class DeferredQueue:
    """The parked questions, on disk.

    On disk rather than in the process because the process that gets refused
    and the process that retries are rarely the same one: Parsa asks in the
    REPL, the REPL exits, and the scheduler is what is still running when the
    window reopens.
    """

    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    @property
    def path(self):
        return self.s.state_dir / "deferred.json"

    # ---- storage ------------------------------------------------------
    def all(self) -> list[Deferred]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for row in raw if isinstance(raw, list) else []:
            try:
                out.append(Deferred(**row))
            except TypeError:          # a row from an older shape is dropped
                continue
        return out

    def _save(self, items: list[Deferred]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---- the queue ----------------------------------------------------
    def park(self, question: str, *, error: str = "", rate_limit: dict | None = None,
             voice: bool = False, now: dt.datetime | None = None) -> Deferred | None:
        """Hold a question for later, or move one already held to the next
        window.

        One method for both because there is only one caller — the escalation
        path — and it cannot tell the two apart: a retry that gets refused is
        the same event as the original refusal, arriving again. Splitting them
        put a second writer on the same entry, and the two disagreed about
        whose retry time was current.

        A question already parked keeps its identity and its `asked_at`, takes
        the new retry time, and spends one of its tries. Returns None when the
        failure is not one that waiting fixes, or when the tries are gone —
        and in that second case the entry is *removed*, so `due()` going quiet
        means the caller has to say something rather than the queue silently
        holding a question forever.
        """
        now = now or dt.datetime.now()
        reason = reason_for(error)
        if not reason or not (question or "").strip():
            return None
        items = self.all()
        text = question.strip()
        when = reset_at(error, rate_limit, now).isoformat(timespec="seconds")

        for existing in items:
            if existing.question.strip() != text:
                continue
            existing.tries += 1
            existing.reason = reason
            if existing.tries >= MAX_TRIES:
                self._save([i for i in items if i.id != existing.id])
                return None
            existing.retry_after = when
            self._save(items)
            return existing

        item = Deferred(question=text, retry_after=when, reason=reason,
                        voice=voice, asked_at=now.isoformat(timespec="seconds"))
        items.append(item)
        self._save(items)
        return item

    def holds(self, question: str) -> bool:
        """Is this question still queued? The answer to 'did the retry take'."""
        text = (question or "").strip()
        return any(i.question.strip() == text for i in self.all())

    def due(self, now: dt.datetime | None = None) -> list[Deferred]:
        now = now or dt.datetime.now()
        return [i for i in self.all() if i.due(now)]

    def drop(self, item_id: str) -> None:
        self._save([i for i in self.all() if i.id != item_id])

    def clear(self) -> None:
        self._save([])
