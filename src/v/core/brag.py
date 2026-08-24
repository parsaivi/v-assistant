"""Brag entries drafted as work closes, not reconstructed at review time.

The failure this fixes is well documented in the vault's own nudge set: by the
time a performance review asks what you shipped, the evidence is six months of
GitLab history you no longer remember the shape of, and what gets written down
is whatever happened in the last three weeks. `nudges.brag_gap` already noticed
the gap every month. Noticing is not the fix.

So: when an issue turns `closed` or an MR turns `merged`, V writes the entry.
Cheap and deterministic — a vault query, a template fill, a git commit. No model
runs here, which also means nothing is invented: every line in a drafted entry
is copied from the issue or MR note it came from, and the two fields that need
judgement (`impact`, `## Reflection`) are left blank on purpose. A brag document
full of a machine's guesses about why your work mattered is worse than an empty
one, because you will read it in six months and not know which parts are true.

**It only writes when there is something to write.** That is the whole
difference between this and a script that turns every merged MR into a note.
`substance()` is the gate: a typo fix, a dependency bump, a revert, an issue
closed with an empty body — those close constantly and are not achievements.
Filing them would bury the ten entries a year that matter under two hundred
that don't, which is the same failure as having no brag document at all.

**Nothing is drafted twice.** `state/brag.json` remembers what it has seen, and
an item already linked from an existing entry is skipped even if the state file
is deleted — the vault is the source of truth, the state file is only a speed-up
and a record of the first-run seed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import notes, query, templates
from .config import Settings, settings as default_settings
from .vaultfs import Vault

# Work that closes without being an achievement. Matched against the title, so
# it is deliberately conservative: a false skip costs one missing draft you can
# still write by hand, a false draft costs trust in the whole document.
TRIVIAL = re.compile(
    r"\b(typo|chore|bump|revert|wip|fixup|lint|format(ting)?|rename|cleanup|"
    r"clean[- ]up|nit|whitespace|comment|gitignore|readme|changelog|"
    r"dependabot|renovate|merge branch|draft)\b", re.I)

# Words that make a review comment praise rather than a change request. Used
# only to pick the entry's `category` and to decide what to quote — never to
# manufacture a compliment that wasn't there.
PRAISE = re.compile(
    r"\b(gg|nice|great|good job|well done|clean|elegant|excellent|thanks|"
    r"thank you|lgtm|👏|🔥|💯|awesome|perfect|beautiful|smooth)\b", re.I)

# A comment block as `update_mr_comments.py` writes it.
COMMENT_HEAD = re.compile(r"^###\s*💬\s*@(\S+)\s*·\s*(\S+)", re.M)

MIN_DESCRIPTION_WORDS = 25


@dataclass
class Candidate:
    rel: str                 # the issue or MR note
    kind: str                # issue | merge-request
    title: str
    closed: dt.date | None
    project: str = ""
    body: str = ""
    reasons: list[str] = field(default_factory=list)   # why it qualified
    skip: str = ""                                     # why it didn't

    @property
    def link(self) -> str:
        return f"[[{self.rel[:-3]}]]"


@dataclass
class Draft:
    candidate: Candidate
    rel: str
    change: object = None


class BragWriter:
    def __init__(self, settings: Settings | None = None,
                 q: query.QueryEngine | None = None,
                 vault: Vault | None = None):
        self.s = settings or default_settings
        self.q = q or query.QueryEngine(self.s)
        self.vault = vault or Vault(self.s)

    # ---- state -------------------------------------------------------
    @property
    def _state_path(self):
        return self.s.state_dir / "brag.json"

    def state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    # ---- what closed --------------------------------------------------
    def closed_work(self) -> list[Candidate]:
        """Every closed issue and merged MR in the vault, newest first."""
        out: list[Candidate] = []
        for row in self.q.issues(status="closed").rows:
            out.append(self._candidate(row, "issue", "closed"))
        for row in self.q.merge_requests(status="merged").rows:
            out.append(self._candidate(row, "merge-request", "merged"))
        out.sort(key=lambda c: c.closed or dt.date.min, reverse=True)
        return out

    def _candidate(self, row, kind: str, date_field: str) -> Candidate:
        body = ""
        try:
            body = self.vault.read(row.rel).body
        except Exception:
            pass
        return Candidate(
            rel=row.rel, kind=kind,
            title=str(row.get("title") or row.title).strip().strip('"'),
            closed=notes.as_date(row.get(date_field)),
            project=str(row.get("project") or "").strip().strip('"'),
            body=body)

    # ---- the gate -----------------------------------------------------
    def already_filed(self, cand: Candidate) -> bool:
        """True when some existing brag entry already links to this item.

        Checked against the vault rather than the state file, so deleting
        `brag.json` re-seeds without producing a second copy of every entry.
        """
        stem = cand.rel[:-3].split("/")[-1]
        for row in self.q.by_type("brag"):
            try:
                links = self.vault.read(row.rel).links
            except Exception:
                continue
            if any(l.split("/")[-1] == stem for l in links):
                return True
        return False

    def substance(self, cand: Candidate) -> Candidate:
        """Decide whether this closure is worth an entry, and say why.

        Sets `cand.reasons` (what makes it one) or `cand.skip` (what doesn't).
        Kept as one readable pass because the *judgement* is the feature — the
        plumbing around it is trivial and the threshold is what you'll want to
        argue with in six months.
        """
        if TRIVIAL.search(cand.title):
            cand.skip = "routine change (title looks like housekeeping)"
            return cand

        note = _note(cand.body)
        description = ""
        for heading in ("Description", "Summary", "Notes / Investigation",
                        "What I did"):
            section = note.section(heading)
            if section:
                description = _clean(section)
                if len(description.split()) >= MIN_DESCRIPTION_WORDS:
                    break

        if len(description.split()) >= MIN_DESCRIPTION_WORDS:
            cand.reasons.append("described")

        comments = _comments(note.section("Comments") or "")
        if comments:
            cand.reasons.append(f"{len(comments)} review comment"
                                f"{'' if len(comments) == 1 else 's'}")
        if any(PRAISE.search(c["text"]) for c in comments):
            cand.reasons.append("praised in review")

        # An issue that shipped through an MR is real work even when nobody
        # wrote much on the issue itself — the MR note carries the evidence.
        linked = [l for l in note.links if l.split("/")[-1].startswith("MR-")]
        if linked:
            cand.reasons.append(f"{len(linked)} linked MR"
                                f"{'' if len(linked) == 1 else 's'}")

        if not cand.reasons:
            cand.skip = ("nothing recorded on it — no description, no review "
                         "comments, no linked MR")
        return cand

    # ---- writing -------------------------------------------------------
    def draft_for(self, cand: Candidate) -> Draft:
        note = _note(cand.body)
        comments = _comments(note.section("Comments") or "")
        praise = [c for c in comments if PRAISE.search(c["text"])]
        day = cand.closed or dt.date.today()

        title = _title(cand.title)
        rel = self._free_path(title)

        meta = {
            "type": "brag",
            "title": title,
            "date": day.isoformat(),
            "category": "recognition" if praise else "delivery",
            "project": cand.project or None,
            # Left blank on purpose. See the module docstring: the one line
            # that says why this mattered is the line only you can write, and a
            # guessed one is indistinguishable from a real one later.
            "impact": None,
            "tags": ["work/brag", "draft"],
        }

        what = ""
        for heading in ("Description", "Summary", "What I did",
                        "Notes / Investigation"):
            section = note.section(heading)
            if section and _clean(section):
                what = _clean(section)
                break

        evidence = [f"- **{'MRs' if cand.kind == 'merge-request' else 'Issues'}:**"
                    f" {cand.link}"]
        for link in note.links:
            stem = link.split("/")[-1]
            if stem.startswith(("MR-", "ISSUE-")):
                evidence.append(f"- {link}")

        filled = templates.fill("brag", meta, title=title, when=day,
                                settings=self.s)
        if filled:
            meta, body = filled
            body = templates.insert_under(body, "What I did", what or
                                          f"Closed {cand.link}.")
            body = templates.insert_under(body, "Evidence", "\n".join(evidence))
            if praise:
                quoted = "\n".join(f"> @{p['who']}: {p['text']}" for p in praise[:3])
                body = templates.insert_under(body, "Praise / Recognition", quoted)
        else:
            body = (f"\n# 🏆 {title}\n\n## What I did\n\n{what}\n\n"
                    f"## Why it mattered / Impact\n\n## Evidence\n"
                    + "\n".join(evidence) + "\n\n## Reflection\n")

        body = (f"\n<!-- Drafted by V from {cand.rel} when it "
                f"{'merged' if cand.kind == 'merge-request' else 'closed'} "
                f"on {day}. Fill in `impact` and Reflection; drop the `draft` "
                f"tag when you have. -->\n" + body)
        change = self.vault.create(
            rel, meta, body, summary=f"draft brag entry from {cand.rel}")
        return Draft(cand, rel, change)

    def _free_path(self, title: str) -> str:
        base = _safe(title)
        rel = f"{self.s.folders.brag}/{base}.md"
        n = 2
        while self.vault.exists(rel):
            rel = f"{self.s.folders.brag}/{base} ({n}).md"
            n += 1
        return rel

    # ---- the job -------------------------------------------------------
    def run(self, *, dry_run: bool = False, backfill: bool = False,
            since: dt.date | None = None, limit: int = 10) -> dict:
        """Draft entries for work that closed since the last run.

        The first run **seeds without writing**. The vault already holds every
        issue and MR closed since June; drafting all of them at once would file
        a hundred entries in one commit and teach you the folder is noise. So
        the first run records what is already closed and drafts nothing, and
        says so. `backfill=True` is the deliberate catch-up.
        """
        state = self.state()
        seen: dict = state.get("seen") or {}
        first_run = not seen and not state.get("seeded")

        candidates = self.closed_work()
        if since:
            candidates = [c for c in candidates if c.closed and c.closed >= since]

        report = {"drafted": [], "skipped": [], "seeded": 0,
                  "dry_run": dry_run, "first_run": first_run}

        if first_run and not backfill:
            state["seen"] = {c.rel: (c.closed or dt.date.today()).isoformat()
                             for c in candidates}
            state["seeded"] = dt.datetime.now().isoformat(timespec="seconds")
            report["seeded"] = len(candidates)
            if not dry_run:
                self._save(state)
            return report

        fresh = [c for c in candidates if backfill or c.rel not in seen]
        for cand in fresh[:limit]:
            self.substance(cand)
            if cand.skip:
                report["skipped"].append({"rel": cand.rel, "why": cand.skip})
            elif self.already_filed(cand):
                report["skipped"].append(
                    {"rel": cand.rel, "why": "already in the brag document"})
            elif dry_run:
                report["drafted"].append(
                    {"rel": cand.rel, "why": ", ".join(cand.reasons),
                     "would_write": self._free_path(_title(cand.title))})
            else:
                draft = self.draft_for(cand)
                report["drafted"].append(
                    {"rel": cand.rel, "why": ", ".join(cand.reasons),
                     "wrote": draft.rel})
            seen[cand.rel] = (cand.closed or dt.date.today()).isoformat()

        if not dry_run:
            state["seen"] = seen
            state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
            state.setdefault("seeded", state.get("seeded")
                             or dt.datetime.now().isoformat(timespec="seconds"))
            self._save(state)
        return report

    def pending_drafts(self) -> list:
        """Drafted entries still carrying the `draft` tag."""
        out = []
        for row in self.q.by_type("brag"):
            tags = row.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            if "draft" in [str(t).lstrip("#") for t in tags]:
                out.append(row)
        return out


# ---- helpers ---------------------------------------------------------
def _note(body: str) -> notes.Note:
    """Wrap a body we already hold, for `.section()` and `.links`.

    Not `notes.parse_text` — that expects a whole file and would read the first
    `---` of a horizontal rule as the start of frontmatter.
    """
    return notes.Note(rel="", path=Path("in-memory.md"), meta={}, body=body)


def _comments(section: str) -> list[dict]:
    """Split a `## Comments` section into `{who, when, text}` blocks."""
    out = []
    heads = list(COMMENT_HEAD.finditer(section))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(section)
        block = section[m.end():end]
        text = " ".join(line.lstrip("> ").strip()
                        for line in block.splitlines()
                        if line.strip().startswith(">"))
        out.append({"who": m.group(1), "when": m.group(2), "text": text.strip()})
    return out


def _clean(section: str) -> str:
    """Strip the scaffolding a template leaves behind: HTML comments, empty
    checkboxes, Dataview inline fields. What's left is what someone wrote."""
    section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    section = re.sub(r"```.*?```", "", section, flags=re.DOTALL)
    lines = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped in ("-", "- [ ]", "|"):
            continue
        if stripped.startswith("**") and "`=" in stripped:
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _title(raw: str) -> str:
    title = raw.strip().strip('"').rstrip(".")
    title = re.sub(r"^(feat|fix|refactor|perf|docs|test|ci)(\([^)]*\))?:\s*", "",
                   title, flags=re.I)
    return (title[:1].upper() + title[1:]) if title else "Untitled"


def _safe(title: str) -> str:
    for ch in '/\\:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip()[:90] or "Untitled"
