"""The email send that checks what Parsa approved, by hash.

Until this module, one sentence in a prompt stood between a draft and a sent
email: "never call gmail_send in the same turn as gmail_draft." A prompt is a
request. A model that misreads "yes, and draft the other one too" as consent,
or that resumes a thread holding a draft id from yesterday, sends anyway, and a
sent email is the one write V makes that nothing can undo.

The fix is Guarded Commits' (Bindschaedler et al., arXiv:2610.00037): an
approval is a stored record bound to the hash of the exact artifact the human
saw, and the code that performs the irreversible action refuses to run without
a matching one.

* `gmail_draft` hashes the draft **as Gmail holds it** — refetched after
  creation, so a header Gmail reformats cannot make every later send fail —
  and appends a `drafted` event here.
* Parsa approves by pressing Send under the reply that shows the draft. That
  text is rendered by code from this ledger, not paraphrased by the model, so
  what he reads is what was hashed. `v mail approve <id>` at a terminal is the
  fallback.
* `Gmail.send` refetches the draft, hashes it again, and consumes a matching
  approval: same hash, unused, younger than `MAX_AGE`. Each approval sends
  once. Anything else is refused with a sentence the model can relay.

The ledger is append-only JSON lines under the state directory: events are
added, never rewritten, so the file is its own audit trail. Nothing a model
can call writes an approval — no MCP tool, no allowlisted tool — and
`approve` refuses outright inside a process a model shell started.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .provenance import refuse_in_model_shell

LEDGER = "mail_approvals.jsonl"
MAX_AGE = dt.timedelta(hours=24)


class Refusal(RuntimeError):
    """A send with no approval that matches the draft as it is now."""


def canonical(to: str, subject: str, body: str, thread_id: str | None) -> str:
    """The fields a reader would call "the email", in one fixed form.

    Normalised only where no reader would see a difference: case and spacing
    in the address list, runs of whitespace in the subject, line endings and
    trailing whitespace in the body. A changed word is a changed hash.
    """
    addrs = ", ".join(a.strip().lower() for a in (to or "").split(",")
                      if a.strip())
    return json.dumps(
        {"to": addrs, "subject": " ".join((subject or "").split()),
         "body": (body or "").replace("\r\n", "\n").rstrip(),
         "thread": thread_id or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def draft_hash(to: str, subject: str, body: str,
               thread_id: str | None = None) -> str:
    return hashlib.sha256(
        canonical(to, subject, body, thread_id).encode("utf-8")).hexdigest()


@dataclass
class Draft:
    draft_id: str
    hash: str
    to: str
    subject: str
    body: str
    thread_id: str = ""
    at: str = ""

    def render(self) -> str:
        """The draft as Parsa sees it above the Send button — built from the
        same fields that were hashed, so the button approves this text."""
        return (f"✉ **Draft to {self.to}** — {self.subject or '(no subject)'}"
                f"\n\n{self.body.rstrip()}\n\n"
                f"_Not sent. Send sends exactly this text "
                f"(sha256 {self.hash[:12]}); an edit made in Gmail after this "
                f"needs a new approval._")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="microseconds")


class Ledger:
    def __init__(self, settings):
        self.s = settings
        self.path: Path = settings.state_dir / LEDGER

    # ---- the file -------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """An exclusive lock for check-then-consume. Two sends of one draft
        racing each other must not both find the approval unused."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _append(self, event: dict) -> dict:
        event = {"at": _now(), **event}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
        return event

    def events(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for line in raw.splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # a torn last line is not a reason to fail
        return out

    # ---- events -----------------------------------------------------------
    def drafted(self, draft: Draft) -> None:
        with self._locked():
            self._append({"event": "drafted", "draft_id": draft.draft_id,
                          "hash": draft.hash, "to": draft.to,
                          "subject": draft.subject, "body": draft.body,
                          "thread_id": draft.thread_id})

    def approve(self, draft_id: str, digest: str, *, by: str,
                via: str) -> dict:
        """Parsa's approval of one draft, bound to one hash. Callers: the
        Send button (`Engine.act`) and `v mail approve`. Nothing else."""
        refuse_in_model_shell()
        if not draft_id or not digest:
            raise Refusal("an approval needs the draft id and its hash")
        with self._locked():
            return self._append({"event": "approved", "id": uuid.uuid4().hex,
                                 "draft_id": draft_id, "hash": digest,
                                 "by": by, "via": via})

    def consume(self, draft_id: str, digest: str,
                now: dt.datetime | None = None) -> dict:
        """The approval this send uses up, or a Refusal saying why not."""
        now = now or dt.datetime.now()
        with self._locked():
            evs = self.events()
            used = {e.get("approval") for e in evs if e.get("event") == "used"}
            mine = [e for e in evs if e.get("event") == "approved"
                    and e.get("draft_id") == draft_id]
            if not mine:
                raise Refusal(
                    f"Parsa has not approved draft {draft_id}. Show him the "
                    "draft and let him press Send under it (or run "
                    f"`v mail approve {draft_id}`). Nothing was sent.")
            same = [e for e in mine if e.get("hash") == digest]
            if not same:
                raise Refusal(
                    f"Draft {draft_id} has changed since Parsa approved it — "
                    "it was edited, in Gmail or elsewhere. His approval was "
                    "for the old text, so it does not cover this one. Show "
                    "him the draft as it is now. Nothing was sent.")
            fresh = [e for e in same if e.get("id") not in used]
            if not fresh:
                raise Refusal(
                    f"Parsa's approval of draft {draft_id} was already used — "
                    "each approval sends once. Nothing was sent.")
            live = [e for e in fresh if now - _parse(e.get("at")) <= MAX_AGE]
            if not live:
                raise Refusal(
                    f"Parsa's approval of draft {draft_id} is more than "
                    f"{int(MAX_AGE.total_seconds() // 3600)} hours old. Ask "
                    "him again. Nothing was sent.")
            approval = live[-1]
            self._append({"event": "used", "approval": approval["id"],
                          "draft_id": draft_id, "hash": digest})
            return approval

    def sent(self, draft_id: str, message_id: str, approval_id: str) -> None:
        with self._locked():
            self._append({"event": "sent", "draft_id": draft_id,
                          "message_id": message_id, "approval": approval_id})

    # ---- reading ----------------------------------------------------------
    def _drafts(self) -> dict[str, Draft]:
        out: dict[str, Draft] = {}
        for e in self.events():
            if e.get("event") == "drafted":
                out[e["draft_id"]] = Draft(
                    e["draft_id"], e.get("hash", ""), e.get("to", ""),
                    e.get("subject", ""), e.get("body", ""),
                    e.get("thread_id", ""), e.get("at", ""))
        return out

    def drafted_since(self, since: str) -> list[Draft]:
        """Drafts created at or after `since` — what a turn just drafted, for
        the Send button on its reply."""
        return [d for d in self._drafts().values() if d.at >= since]

    def pending(self) -> list[Draft]:
        """Drafts V made that have not been sent."""
        sent = {e.get("draft_id") for e in self.events()
                if e.get("event") == "sent"}
        return [d for d in self._drafts().values() if d.draft_id not in sent]


def _parse(at) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(at))
    except ValueError:
        return dt.datetime.min
