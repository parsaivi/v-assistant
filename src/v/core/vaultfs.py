"""Every write to the vault goes through here. Nothing else touches the disk.

Three rules, from the design doc:
  1. Append-only to existing notes. Structural rewrites need `allow_rewrite=True`,
     which the engine only sets after an explicit confirmation.
  2. Every change is a git commit prefixed `[bot]`, so `git log --grep='\\[bot\\]'`
     is a complete record of what the assistant did.
  3. Every change is undoable for as long as the git history exists.
"""

from __future__ import annotations

import datetime as dt
import re
import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from . import notes
from .config import Settings, settings as default_settings


class VaultError(RuntimeError):
    pass


@dataclass
class Change:
    """A recorded vault mutation, enough to describe and undo it."""

    kind: str               # append | create | rewrite | move | delete
    rel: str
    commit: str | None
    summary: str
    at: str
    bytes_added: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Vault:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        self.root = self.s.vault
        self._history: list[Change] = []

    # ---- reading ----------------------------------------------------
    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise VaultError(f"path escapes the vault: {rel}")
        return p

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def read(self, rel: str) -> notes.Note:
        p = self.path(rel)
        if not p.exists():
            raise VaultError(f"no such note: {rel}")
        return notes.parse(p, self.root)

    def all_notes(self):
        for p in notes.iter_notes(self.root):
            try:
                yield notes.parse(p, self.root)
            except OSError:
                continue

    # ---- git --------------------------------------------------------
    @property
    def git_available(self) -> bool:
        return self.s.git_enabled and (self.root / ".git").exists()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=check,
        )

    def _commit(self, rel: str, summary: str) -> str | None:
        if not self.git_available:
            return None
        try:
            self._git("add", "--", rel)
            status = self._git("status", "--porcelain", "--", rel).stdout.strip()
            if not status:
                return None
            msg = f"{self.s.commit_prefix} {summary}"
            self._git(
                "-c", f"user.name={self.s.git_author_name}",
                "-c", f"user.email={self.s.git_author_email}",
                "commit", "-q", "-m", msg, "--", rel,
            )
            return self._git("rev-parse", "HEAD").stdout.strip()[:12]
        except subprocess.CalledProcessError as exc:
            raise VaultError(f"git failed: {exc.stderr.strip()}") from exc

    # ---- the working tree before V touches it ------------------------
    @property
    def _scope(self) -> list[str]:
        """The pathspec `commit_pending` works over: the vault, minus V's own
        source tree. `git status` understands `:(exclude)`, so the filtering
        happens in git rather than in a second pass here."""
        return [".", *(f":(exclude){p}" for p in
                       self.s.git_commit_on_start_exclude)]

    def pending(self) -> list[str]:
        """Paths git would commit right now. Honours .gitignore, so the
        assistant's own state directory never shows up here."""
        if not self.git_available:
            return []
        out = self._git("status", "--porcelain", "--", *self._scope,
                        check=False).stdout
        rels = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:          # a rename; the new name is the one that exists
                path = path.split(" -> ", 1)[1]
            rels.append(path.strip('"'))
        return rels

    def _mid_operation(self) -> bool:
        """A half-finished merge/rebase/revert. Committing into one of those
        turns a recoverable state into a confusing one."""
        g = self.root / ".git"
        return any((g / n).exists() for n in (
            "MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD",
            "rebase-merge", "rebase-apply"))

    def commit_pending(self, *, summary: str | None = None) -> Change | None:
        """Commit hand-written changes before the session starts.

        Under the *repository's* identity and with no `[bot]` prefix, which is
        the whole point: these are Parsa's notes, not V's work. Folding them
        into a bot commit would put them in the audit trail as something the
        assistant did and inside the blast radius of `undo`.

        Returns None when there was nothing to commit — the common case.
        """
        if not self.git_available or self._mid_operation():
            return None
        rels = self.pending()
        if not rels:
            return None
        n = len(rels)
        summary = summary or (
            f"vault: {n} uncommitted change{'' if n == 1 else 's'} "
            "before assistant session")
        try:
            self._git("add", "-A", "--", *self._scope)
            # Path-limited, so anything staged outside the scope — a source
            # change the user had `git add`ed and not yet committed — stays
            # staged and uncommitted rather than riding along.
            self._git("commit", "-q", "-m", summary, "--", *self._scope)
            commit = self._git("rev-parse", "HEAD").stdout.strip()[:12]
        except subprocess.CalledProcessError as exc:
            raise VaultError(
                f"could not commit the working tree: "
                f"{(exc.stderr or exc.stdout or '').strip()}") from exc
        # Logged but deliberately kept out of `_history`: a bare `undo` takes
        # the last entry there, and the last thing the user wants reverted is
        # the note they hand-wrote this morning.
        return self._record(Change(
            "commit", rels[0] if n == 1 else f"{n} files", commit, summary,
            dt.datetime.now().isoformat(timespec="seconds")), undoable=False)

    def _preserve(self, rel: str) -> None:
        """Commit a hand-written change to *this* note before writing over it.

        `commit_pending` handles the CLI, which has a start. The MCP server
        does not — it connects once and runs for days, so a note edited in
        Obsidian at noon is still uncommitted when V appends to it at three,
        and the resulting `[bot]` commit contains both. Path-limited on
        purpose: a write to one note must not sweep up unrelated
        work-in-progress elsewhere in the vault.

        Best-effort. Failing to tidy history is not a reason to refuse a write.
        """
        if not self.git_available or self._mid_operation():
            return
        if not self._git("status", "--porcelain", "--", rel,
                         check=False).stdout.strip():
            return
        try:
            self._git("add", "--", rel)
            self._git("commit", "-q", "-m",
                      f"vault: hand-written changes to {rel}", "--", rel)
        except subprocess.CalledProcessError:
            pass

    def _record(self, change: Change, *, undoable: bool = True) -> Change:
        if undoable:
            self._history.append(change)
        log = self.s.log_dir / "changes.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(change.to_dict(), ensure_ascii=False) + "\n")
        return change

    # ---- writing ----------------------------------------------------
    def append(self, rel: str, text: str, *, heading: str | None = None,
               summary: str | None = None) -> Change:
        """Append to a note, creating it if absent. The safe default everywhere."""
        self._preserve(rel)
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = text.rstrip("\n")

        if not p.exists():
            content = (f"## {heading}\n\n{text}\n" if heading else f"{text}\n")
            p.write_text(content, encoding="utf-8")
            kind = "create"
        else:
            existing = p.read_text(encoding="utf-8")
            if heading:
                note = notes.parse(p, self.root)
                if note.section(heading) is not None:
                    existing = append_under_heading(existing, heading, text)
                else:
                    existing = existing.rstrip("\n") + f"\n\n## {heading}\n\n{text}\n"
            else:
                existing = existing.rstrip("\n") + f"\n{text}\n"
            p.write_text(existing, encoding="utf-8")
            kind = "append"

        summary = summary or f"{kind} {rel}"
        return self._record(Change(kind, rel, self._commit(rel, summary), summary,
                                   dt.datetime.now().isoformat(timespec="seconds"),
                                   len(text.encode())))

    def create(self, rel: str, meta: dict, body: str, *, overwrite: bool = False,
               summary: str | None = None) -> Change:
        p = self.path(rel)
        if p.exists() and not overwrite:
            raise VaultError(f"refusing to overwrite existing note: {rel}")
        self._preserve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        note = notes.Note(rel=rel, path=p, meta=meta, body=body)
        p.write_text(note.render(), encoding="utf-8")
        summary = summary or f"create {rel}"
        return self._record(Change("create", rel, self._commit(rel, summary), summary,
                                   dt.datetime.now().isoformat(timespec="seconds"),
                                   p.stat().st_size))

    def save_binary(self, rel: str, data: bytes, *,
                    summary: str | None = None) -> Change:
        """Write raw bytes — a downloaded Telegram file, an image — under the
        same rules as `create`: refuses to clobber, commits `[bot]`, revertible.

        Separate from `create` rather than a bytes-or-str branch there, because
        a binary file has no frontmatter and `notes.Note.render()` would
        corrupt it if this ever fell through to that path.
        """
        p = self.path(rel)
        if p.exists():
            raise VaultError(f"refusing to overwrite existing file: {rel}")
        self._preserve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        summary = summary or f"save {rel}"
        return self._record(Change("create", rel, self._commit(rel, summary), summary,
                                   dt.datetime.now().isoformat(timespec="seconds"),
                                   len(data)))

    def set_meta(self, rel: str, updates: dict, *, summary: str | None = None) -> Change:
        """Update frontmatter only. Body untouched — the one safe structural edit."""
        self._preserve(rel)
        note = self.read(rel)
        note.meta.update(updates)
        self.path(rel).write_text(note.render(), encoding="utf-8")
        summary = summary or f"update frontmatter of {rel} ({', '.join(updates)})"
        return self._record(Change("rewrite", rel, self._commit(rel, summary), summary,
                                   dt.datetime.now().isoformat(timespec="seconds")))

    def edit(self, rel: str, old: str, new: str, *,
             summary: str | None = None) -> Change:
        """Replace one exact, unique run of text inside an existing note.

        The safe shape of "edit", and deliberately not `rewrite`. A model
        asked to rewrite a whole file has to reproduce every line it did not
        mean to change, and a single dropped paragraph is indistinguishable
        from an intended deletion — the note still parses, the dashboards
        still query it, and nothing looks wrong until Parsa goes looking for
        something that is gone. Matching a literal string instead means the
        model states precisely what it is changing and the writer verifies it
        before touching the disk.

        Two refusals, both load-bearing:

        * **not found** — the model is working from a stale or imagined copy
          of the note, which is exactly when an edit does the most damage.
        * **more than one match** — it cannot know which one was meant, and
          picking the first is a coin flip on the user's data.

        Recoverable like every other write here: `[bot]`-committed per path,
        reverted by `undo`. That is what makes it defensible at all.
        """
        if not old:
            raise VaultError("edit needs the text to replace")
        if old == new:
            raise VaultError("old and new are identical — nothing to do")
        p = self.path(rel)
        if not p.exists():
            raise VaultError(f"{rel} does not exist — use create or append")
        self._preserve(rel)
        text = p.read_text(encoding="utf-8")
        hits = text.count(old)
        if hits == 0:
            raise VaultError(
                f"that text is not in {rel}. Read the note again — you are "
                "editing a version of it that does not exist.")
        if hits > 1:
            raise VaultError(
                f"that text appears {hits} times in {rel}; include enough "
                "surrounding lines to make it unique.")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        summary = summary or f"edit {rel}"
        return self._record(Change("edit", rel, self._commit(rel, summary),
                                   summary,
                                   dt.datetime.now().isoformat(timespec="seconds")))

    def rewrite(self, rel: str, content: str, *, allow_rewrite: bool = False,
                summary: str | None = None) -> Change:
        if not allow_rewrite:
            raise VaultError(
                "rewrite requires explicit confirmation (allow_rewrite=True); "
                "the assistant appends by default"
            )
        self._preserve(rel)
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        summary = summary or f"rewrite {rel}"
        return self._record(Change("rewrite", rel, self._commit(rel, summary), summary,
                                   dt.datetime.now().isoformat(timespec="seconds")))

    # ---- undo -------------------------------------------------------
    def undo(self, commit: str | None = None) -> str:
        """Revert a bot commit. Defaults to the most recent one this session made."""
        if not self.git_available:
            raise VaultError("git is not enabled; nothing to undo")
        if commit is None:
            for change in reversed(self._history):
                if change.commit:
                    commit = change.commit
                    break
        if commit is None:
            commit = self._last_bot_commit()
        if not commit:
            raise VaultError("no bot commit found to undo")

        msg = self._git("log", "-1", "--format=%s", commit).stdout.strip()
        self._git("-c", f"user.name={self.s.git_author_name}",
                  "-c", f"user.email={self.s.git_author_email}",
                  "revert", "--no-edit", commit)
        return f"reverted {commit} — {msg}"

    @property
    def _bot_grep(self) -> str:
        # The prefix is `[bot]`, and git --grep takes a regex — unescaped, that
        # is a character class matching 'b', 'o' or 't', which never matches a
        # subject starting with '['. Silently found nothing.
        return "^" + re.escape(self.s.commit_prefix)

    def _last_bot_commit(self) -> str | None:
        out = self._git(
            "log", "--grep", self._bot_grep, "--format=%h", "-1", check=False,
        ).stdout.strip()
        return out or None

    def bot_log(self, limit: int = 20) -> list[str]:
        if not self.git_available:
            return []
        out = self._git("log", "--grep", self._bot_grep,
                        "--format=%h %ad %s", "--date=short", f"-{limit}",
                        check=False).stdout
        return [line for line in out.splitlines() if line.strip()]

    @property
    def history(self) -> list[Change]:
        return list(self._history)


def append_under_heading(content: str, heading: str, text: str) -> str:
    """Insert `text` at the end of an existing section, before the next heading.

    Public because template filling needs it on a note body that has not been
    written yet — `capture` drops the captured text under the right heading of
    a freshly rendered template before the file exists at all.
    """
    lines = content.splitlines()
    target = None
    level = 2
    for i, line in enumerate(lines):
        m = notes.HEADING.match(line)
        if m and m.group(2).strip().lower() == heading.strip().lower():
            target = i
            level = len(m.group(1))
            break
    if target is None:
        return content.rstrip("\n") + f"\n\n## {heading}\n\n{text}\n"

    end = len(lines)
    for j in range(target + 1, len(lines)):
        m = notes.HEADING.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    while end > target + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = [text]
    return "\n".join(lines) + "\n"
