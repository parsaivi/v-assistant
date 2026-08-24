"""GitLab / work integration. Reads `glab` and git; writes schema-valid notes.

The important decision here is that turning `glab issue list --output json` into
`ISSUE-<id>` notes is a **template fill, not a reasoning task**. Python does it
for $0, deterministically, with frontmatter that always matches the schema —
which an LLM cannot promise. The cost model says this alone cuts agentic runs
from ~3/day to ~1/day.

`fetch_issues.py`, `fetch_mrs.py` and `update_mr_comments.py` already exist at
the vault root; this module complements them rather than replacing them.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import notes, query, schema, templates
from .config import Settings, settings as default_settings
from .vaultfs import Vault

# GitLab's API vocabulary is not the vault's. This mapping is exactly the bug
# the schema validator found: 16 issue notes carry `opened` and are therefore
# invisible to every Dataview dashboard.
STATUS_MAP = {
    "opened": "open", "open": "open",
    "closed": "closed", "merged": "merged", "locked": "closed",
}


@dataclass
class GitLabItem:
    iid: int
    title: str
    state: str
    project: str
    created: str
    url: str
    extra: dict = field(default_factory=dict)


class Work:
    def __init__(self, settings: Settings | None = None, vault: Vault | None = None):
        self.s = settings or default_settings
        self.vault = vault or Vault(self.s)

    # ---- glab --------------------------------------------------------
    @property
    def glab_available(self) -> bool:
        try:
            subprocess.run(["glab", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _glab(self, *args: str, repo: str | None = None) -> list[dict]:
        cmd = ["glab", *args, "--output", "json"]
        if repo:
            cmd += ["--repo", repo]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else [data]

    def fetch_issues(self, repo: str | None = None) -> list[GitLabItem]:
        raw = self._glab("issue", "list", "--assignee", "@me", "--all", repo=repo)
        return [self._item(r, "issue") for r in raw]

    def fetch_mrs(self, repo: str | None = None) -> list[GitLabItem]:
        raw = self._glab("mr", "list", "--author", "@me", "--all", repo=repo)
        return [self._item(r, "mr") for r in raw]

    def _item(self, r: dict, kind: str) -> GitLabItem:
        url = r.get("web_url") or ""
        project = ""
        if url:
            parts = url.split("/-/")[0].rstrip("/").split("/")
            project = parts[-1] if parts else ""
        return GitLabItem(
            iid=r.get("iid") or r.get("id") or 0,
            title=(r.get("title") or "").strip(),
            state=STATUS_MAP.get(str(r.get("state", "")).lower(), "open"),
            project=project,
            created=(r.get("created_at") or "")[:10],
            url=url,
            extra=r,
        )

    # ---- note generation (template fill, $0) --------------------------
    #
    # "Template fill" is now literal: the body comes from `05 - Templates/`.
    # The hand-written bodies these methods used to return were missing the
    # Acceptance Criteria checklist, the Related section and the Activity Log
    # table that every issue note written through Obsidian has — so a note V
    # created was recognisably V's, and the parts you'd actually fill in by
    # hand weren't there to fill in.
    def issue_note(self, item: GitLabItem) -> tuple[str, dict, str]:
        rel = f"{self.s.folders.issues}/ISSUE-{item.iid} {_safe(item.title)}.md"
        meta = schema.template_for("issue")
        meta.update({
            "issue-id": item.iid,
            "title": item.title,
            "project": item.project,
            "status": item.state,
            "priority": _priority(item.extra) or "medium",
            "estimate": None,
            "sprint": (item.extra.get("milestone") or {}).get("title")
            if isinstance(item.extra.get("milestone"), dict) else None,
            "assignee": "me",
            "created": item.created,
            "closed": (item.extra.get("closed_at") or "")[:10] or None,
            "gitlab-url": item.url,
            "tags": ["work/issue"],
        })
        description = (item.extra.get("description") or "").strip()
        filled = templates.fill("issue", meta, title=item.title,
                                when=notes.as_date(item.created),
                                settings=self.s)
        if filled:
            meta, body = filled
            if description:
                body = templates.insert_under(body, "Description", description)
            return rel, meta, body
        body = (f"\n## Description\n\n{description}\n"
                "\n## Notes / Investigation\n\n## Activity Log\n")
        return rel, meta, body

    def mr_note(self, item: GitLabItem) -> tuple[str, dict, str]:
        rel = f"{self.s.folders.mrs}/MR-{item.iid} {_safe(item.title)}.md"
        meta = schema.template_for("merge-request")
        meta.update({
            "mr-id": item.iid, "title": item.title, "project": item.project,
            "status": item.state, "created": item.created,
            "merged": (item.extra.get("merged_at") or "")[:10] or None,
            "branch": item.extra.get("source_branch"),
            "target-branch": item.extra.get("target_branch"),
            "reviewer": _reviewer(item.extra),
            "gitlab-url": item.url,
            "tags": ["work/mr"],
        })
        filled = templates.fill("merge-request", meta, title=item.title,
                                when=notes.as_date(item.created),
                                settings=self.s)
        if filled:
            meta, body = filled
            description = (item.extra.get("description") or "").strip()
            if description:
                body = templates.insert_under(body, "Summary", description)
            return rel, meta, body
        body = "\n## Description\n\n## Comments\n\n## Activity Log\n"
        return rel, meta, body

    def sync(self, *, dry_run: bool = True, repo: str | None = None) -> dict:
        """Create missing notes and correct drifted frontmatter. Never rewrites bodies."""
        report = {"created": [], "updated": [], "skipped": 0, "dry_run": dry_run}
        if not self.glab_available:
            report["error"] = "glab not available"
            return report

        for items, builder in ((self.fetch_issues(repo), self.issue_note),
                               (self.fetch_mrs(repo), self.mr_note)):
            for item in items:
                rel, meta, body = builder(item)
                if self.vault.exists(rel):
                    note = self.vault.read(rel)
                    drift = {k: v for k, v in meta.items()
                             if k in ("status", "merged", "closed")
                             and v not in (None, "")
                             and str(note.meta.get(k) or "").strip('"') != str(v)}
                    if drift:
                        if not dry_run:
                            self.vault.set_meta(rel, drift,
                                                summary=f"sync {rel} from GitLab")
                        report["updated"].append({"rel": rel, "changes": drift})
                    else:
                        report["skipped"] += 1
                else:
                    if not dry_run:
                        self.vault.create(rel, meta, body,
                                          summary=f"create {rel} from GitLab")
                    report["created"].append(rel)
        return report

    # ---- schema repair -------------------------------------------------
    def fix_status_values(self, *, dry_run: bool = True) -> dict:
        """Repair GitLab's vocabulary leaking into vault frontmatter.

        `opened` is not a valid `status` per CLAUDE.md, so any note carrying it
        is silently absent from every dashboard query.
        """
        q = query.QueryEngine(self.s)
        fixed = []
        for type_name, valid in (("issue", schema.SCHEMAS["issue"]),
                                 ("merge-request", schema.SCHEMAS["merge-request"])):
            allowed = valid.field("status").enum or ()
            for row in q.by_type(type_name):
                raw = str(row.get("status") or "").strip().strip('"')
                if raw and raw not in allowed:
                    mapped = STATUS_MAP.get(raw.lower())
                    if mapped and mapped in allowed:
                        fixed.append({"rel": row.rel, "from": raw, "to": mapped})
                        if not dry_run:
                            self.vault.set_meta(
                                row.rel, {"status": mapped},
                                summary=f"normalise status {raw} → {mapped}")
        q.close()
        return {"fixed": fixed, "count": len(fixed), "dry_run": dry_run}

    # ---- standup -------------------------------------------------------
    def commits_since(self, since: dt.datetime, repo: Path | None = None) -> list[str]:
        repo = repo or self.s.vault
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", "--no-merges",
                 f"--since={since.isoformat()}", "--format=%s"],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        return [l for l in out.stdout.splitlines()
                if l.strip() and not l.startswith(self.s.commit_prefix)]

    def standup(self, *, days: int = 1) -> str:
        """Yesterday / today / blockers, from vault state. No model."""
        q = query.QueryEngine(self.s)
        since = dt.datetime.now() - dt.timedelta(days=days)
        commits = self.commits_since(since)

        recent_mrs = [m for m in q.merge_requests().rows
                      if str(m.get("created") or "").strip('"')
                      >= since.date().isoformat()]
        in_progress = q.issues(status="in-progress").rows
        in_review = q.issues(status="in-review").rows
        open_issues = q.issues(status="open").rows
        q.close()

        lines = ["**Yesterday**"]
        if commits:
            lines += [f"- {c}" for c in commits[:6]]
        if recent_mrs:
            lines += [f"- MR-{m.get('mr-id')} {m.get('title')}" for m in recent_mrs[:4]]
        if not commits and not recent_mrs:
            lines.append("- (nothing recorded in the vault)")

        lines.append("\n**Today**")
        todo = in_progress or open_issues
        lines += ([f"- ISSUE-{i.get('issue-id')} {i.get('title')}" for i in todo[:4]]
                  or ["- (nothing in-progress)"])

        lines.append("\n**Blockers**")
        lines += ([f"- ISSUE-{i.get('issue-id')} waiting on review"
                   for i in in_review[:4]] or ["- none"])
        return "\n".join(lines)


def _safe(title: str) -> str:
    """Filenames in this vault keep Persian text; only path separators are unsafe."""
    for ch in '/\\:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip()[:90]


def _priority(raw: dict) -> str | None:
    labels = [str(l).lower() for l in (raw.get("labels") or [])]
    for level in ("critical", "high", "medium", "low"):
        if any(level in l for l in labels):
            return level
    return None


def _reviewer(raw: dict) -> str | None:
    revs = raw.get("reviewers") or []
    if isinstance(revs, list) and revs:
        first = revs[0]
        return first.get("username") if isinstance(first, dict) else str(first)
    return None
