"""One conversation, from the moment V starts to the moment you end it.

Two things were broken and they were the same thing. V held no conversation:
every message opened a fresh thread, so "set a 1on1 with him" arrived at the
model with no idea who "him" was, having just answered a question about Arash's
calendar thirty seconds earlier. And when V closed, the exchange was gone —
`state/logs/*.jsonl` recorded what you *asked* but never what V *answered*, so
there was nothing left to look back at.

A session fixes both by being one object: the thread the model resumes, and the
transcript that gets written to the vault when the thread ends. It stays open
until you end it — `/new`, or quitting V — and not one second less. The old
30-minute cold-thread expiry does not apply to an open session; an idle gap is
not you saying you were finished.

The price of that is real and worth stating: resuming a thread re-sends its
whole history on every turn, so the tenth message in a session costs more than
the first. That is the trade being made deliberately here — `/new` is the way
to stop paying for a topic you have moved on from.

Nothing in this module writes to the vault or calls a model. It records turns
to `state/sessions/<id>.jsonl` as they happen and renders them to Markdown with
string formatting. That is deliberate: a recap produced by asking a model to
summarise would cost allowance every time you quit, and would fail exactly when
the model is down or rate-limited — which is one of the moments you most want
the transcript. So the recap *is* the output. Because the transcript is written
per turn rather than at the end, a crash or a killed terminal loses nothing:
the jsonl is already on disk and the next run flushes it (see `orphans`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, settings as default_settings


@dataclass
class Turn:
    """One exchange. `answer` is what V actually said, which is the half the
    old JSONL log threw away."""
    at: str
    text: str
    answer: str
    route: str = ""
    intent: str = ""
    ms: float = 0.0
    cost: float = 0.0        # what you were billed, 0 on a plan
    listed: float = 0.0      # list price of the same call, for the accounting
    billing: str = ""
    thread: str = ""         # the model's own session id, when one was reached

    @classmethod
    def from_row(cls, row: dict) -> "Turn":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in fields})


@dataclass
class Session:
    id: str
    started: str
    pid: int
    open: bool = True

    @property
    def started_at(self) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(self.started)
        except ValueError:
            return None


@dataclass
class Recap:
    """A closed session, rendered. `markdown` is what goes in the vault;
    `path` is the raw transcript that exists whether or not that write
    succeeds."""
    session: Session
    turns: list[Turn]
    day: dt.date
    reason: str
    markdown: str
    path: Path
    cost: float = 0.0
    listed: float = 0.0
    filed: bool = False      # did it reach the vault, or only `path`?

    @property
    def n(self) -> int:
        return len(self.turns)


def is_open(settings: Settings | None = None) -> bool:
    """Is a conversation running right now?

    Deliberately a module-level read of one small file rather than a method, so
    `hermes` can ask without owning a store — it needs the answer on every
    escalation and must not grow a dependency on the vault to get it.
    """
    return SessionStore(settings).current() is not None


class SessionStore:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    # ---- paths --------------------------------------------------------
    @property
    def _state_path(self) -> Path:
        return self.s.state_dir / "session.json"

    def transcript_path(self, sid: str) -> Path:
        return self.s.state_dir / "sessions" / f"{sid}.jsonl"

    # ---- state --------------------------------------------------------
    def _load(self) -> Session | None:
        try:
            row = json.loads(self._state_path.read_text(encoding="utf-8"))
            return Session(id=row["id"], started=row["started"],
                           pid=int(row.get("pid") or 0),
                           open=bool(row.get("open", True)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save(self, session: Session) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(dataclasses.asdict(session), indent=2), encoding="utf-8")

    def current(self) -> Session | None:
        """The open session, if there is one. No clock anywhere in here — a
        session ends because you ended it."""
        s = self._load()
        return s if s and s.open else None

    # ---- lifecycle ----------------------------------------------------
    def start(self, now: dt.datetime | None = None) -> Session:
        now = now or dt.datetime.now()
        # The suffix is not decoration. `/new` ends one session and opens the
        # next in the same second, and a second-resolution id would give both
        # the same transcript file — the new conversation would open holding
        # the old one's turns and file them a second time.
        session = Session(id=f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
                          started=now.isoformat(timespec="seconds"),
                          pid=os.getpid())
        self._save(session)
        return session

    def orphans(self, now: dt.datetime | None = None) -> list[Recap]:
        """Sessions a previous run left open, closed out now.

        A `kill -9`, a closed laptop or a crashed terminal never reaches the
        exit path, so its transcript sits on disk with nothing to write it out.
        Rather than lose it, the next run adopts it: anything marked open that
        does not belong to this process gets rendered and returned for the
        caller to file. Called before `start`, so the recap lands under the day
        the conversation actually happened on, not today's log.
        """
        s = self._load()
        if not s or not s.open or s.pid == os.getpid():
            return []
        recap = self.close("interrupted", now=now, session=s)
        return [recap] if recap else []

    def close(self, reason: str = "quit", now: dt.datetime | None = None,
              session: Session | None = None) -> Recap | None:
        """End the conversation and render it. Returns None for a session with
        nothing in it — starting V and quitting without saying anything is not
        an event, and a log full of empty stubs is a log nobody reads."""
        now = now or dt.datetime.now()
        s = session or self.current()
        if not s:
            return None
        # Marked closed before anything can fail. If rendering or the vault
        # write throws, the session must still be over — otherwise every
        # subsequent run re-adopts the same orphan and writes it again.
        self._save(dataclasses.replace(s, open=False))

        turns = self.turns(s.id)
        if not turns:
            return None
        day = (s.started_at or now).date()
        return Recap(session=s, turns=turns, day=day, reason=reason,
                     markdown=render(s, turns, ended=now, reason=reason),
                     path=self.transcript_path(s.id),
                     cost=sum(t.cost for t in turns),
                     listed=sum(t.listed for t in turns))

    # ---- turns --------------------------------------------------------
    def record(self, text: str, resp, now: dt.datetime | None = None) -> None:
        """Append one exchange. A no-op when no session is open, which is the
        normal state for the MCP server, `send` from a script and the
        scheduler — none of those are a conversation anyone will end."""
        s = self.current()
        if not s:
            return
        now = now or dt.datetime.now()
        data = getattr(resp, "data", None) or {}
        turn = Turn(
            at=now.isoformat(timespec="seconds"),
            text=text,
            answer=getattr(resp, "text", "") or "",
            route=getattr(resp, "route", "") or "",
            intent=getattr(resp, "intent", "") or "",
            ms=round(getattr(resp, "ms", 0.0) or 0.0, 1),
            cost=getattr(resp, "cost", 0.0) or 0.0,
            listed=data.get("list_cost_usd") or 0.0,
            billing=data.get("billing") or "",
            thread=data.get("session") or "",
        )
        path = self.transcript_path(s.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dataclasses.asdict(turn),
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass  # a failed transcript write must not take down the answer

    def turns(self, sid: str) -> list[Turn]:
        path = self.transcript_path(sid)
        out: list[Turn] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    out.append(Turn.from_row(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        except OSError:
            return []
        return out


# ---- rendering --------------------------------------------------------
def render(session: Session, turns: list[Turn], *, ended: dt.datetime,
           reason: str = "quit") -> str:
    """The recap, built from strings.

    Every line of what was said, not a summary of it: summarising needs a
    model, a model costs allowance on a plan shared with your normal usage, and
    the one time you most want to read back a session is when the model just
    failed you. Bodies are quoted rather than indented so a `#` or a `-` in
    something you typed cannot forge a heading in the daily log.
    """
    started = session.started_at
    head = (f"### {started.strftime('%H:%M')} – {ended.strftime('%H:%M')}"
            if started else f"### session {session.id}")
    head += f" · {len(turns)} turn{'s' if len(turns) != 1 else ''}"

    listed = sum(t.listed for t in turns)
    billed = sum(t.cost for t in turns)
    if billed:
        head += f" · ${billed:.4f}"
    elif listed:
        head += f" · ≈${listed:.4f} of plan allowance"
    else:
        head += " · free"

    lines = [head, ""]
    routes: dict[str, int] = {}
    for t in turns:
        # The head of the route only. An unrouted capture carries the router's
        # whole explanation ("capture.note (60%) — nothing specific matched…"),
        # which belongs on the turn it describes, not in a one-line tally.
        head = (t.route or "?").split(" ")[0]
        routes[head] = routes.get(head, 0) + 1
    summary = " · ".join(f"`{r}` ×{n}" if n > 1 else f"`{r}`"
                         for r, n in sorted(routes.items(), key=lambda kv: -kv[1]))
    threads = [t.thread for t in turns if t.thread]
    if threads:
        summary += f" · thread `{threads[-1][:8]}`"
    lines += [f"{summary} · ended: {reason}", ""]

    for t in turns:
        clock = t.at[11:16] if len(t.at) >= 16 else ""
        lines += [f"**you** · {clock}", _quote(t.text), ""]
        tail = f" · `{t.route}`" if t.route else ""
        if t.cost:
            tail += f" · ${t.cost:.4f}"
        elif t.listed:
            tail += f" · ≈${t.listed:.4f}"
        lines += [f"**V** · {clock}{tail}", _quote(t.answer), ""]
    return "\n".join(lines).rstrip("\n")


def _quote(text: str) -> str:
    """Markdown-quote a body so nothing inside it can restructure the note it
    is being appended to."""
    text = (text or "").strip() or "_(nothing)_"
    return "\n".join(f"> {line}" if line.strip() else ">"
                     for line in text.splitlines())
