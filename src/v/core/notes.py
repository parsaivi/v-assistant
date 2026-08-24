"""Markdown + YAML frontmatter handling.

Deliberately not `python-frontmatter`: this is ~80 lines, has no dependency, and
round-trips the vault's exact style (quoted dates, `---` fences, trailing
newline) without reformatting notes the user hand-wrote.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FENCE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
TAG = re.compile(r"(?:^|\s)#([A-Za-z؀-ۿ][\w؀-ۿ/-]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _yaml_str(value: Any) -> str:
    """Serialise a single value the way the vault writes it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return f'"{value.isoformat()[:10]}"'
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_str(v) for v in value) + "]"
    if value is None:
        return ""
    s = str(value)
    # Dates and anything with YAML-significant characters get quoted.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.search(r"[:#\[\]{}&*!|>'\"%@`]", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def dump_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list) and v and all(isinstance(i, str) for i in v) and len(v) > 3:
            lines.append(f"{k}:")
            lines.extend(f"  - {i}" for i in v)
        else:
            rendered = _yaml_str(v)
            lines.append(f"{k}:{' ' + rendered if rendered else ''}")
    lines.append("---")
    return "\n".join(lines) + "\n"


@dataclass
class Note:
    """A parsed vault note. `rel` is the vault-relative path, the primary key."""

    rel: str
    path: Path
    meta: dict = field(default_factory=dict)
    body: str = ""
    mtime: float = 0.0

    # ---- derived ----------------------------------------------------
    @property
    def title(self) -> str:
        return self.meta.get("title") or self.path.stem

    @property
    def type(self) -> str | None:
        t = self.meta.get("type")
        return str(t) if t else None

    @property
    def folder(self) -> str:
        return str(Path(self.rel).parent).replace("\\", "/")

    @property
    def links(self) -> list[str]:
        # Code must be stripped first: Dataview blocks are full of string
        # concatenation that looks exactly like a wikilink to a regex.
        body = _strip_code(self.body)
        out = []
        for m in WIKILINK.finditer(body):
            # Inside a markdown table an alias pipe is escaped as `\|`, so the
            # target captured here keeps a trailing backslash. Left in, it makes
            # every table wikilink look broken.
            target = m.group(1).strip().rstrip("\\").strip()
            if not target or target.endswith("/"):
                continue  # folder link, not a note
            out.append(target)
        return out

    @property
    def tags(self) -> list[str]:
        inline = {m.group(1) for m in TAG.finditer(_strip_code(self.body))}
        fm = self.meta.get("tags") or []
        if isinstance(fm, str):
            fm = [t.strip() for t in fm.replace(",", " ").split()]
        return sorted(inline | {str(t).lstrip("#") for t in fm})

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    def section(self, heading: str) -> str | None:
        """Return the body of a `## heading` section, exclusive of the heading."""
        matches = list(HEADING.finditer(self.body))
        for i, m in enumerate(matches):
            if m.group(2).strip().lower() == heading.strip().lower():
                start = m.end()
                level = len(m.group(1))
                for nxt in matches[i + 1:]:
                    if len(nxt.group(1)) <= level:
                        return self.body[start:nxt.start()].strip()
                return self.body[start:].strip()
        return None

    def render(self) -> str:
        head = dump_frontmatter(self.meta) if self.meta else ""
        body = self.body if self.body.endswith("\n") else self.body + "\n"
        return head + ("\n" if head and not body.startswith("\n") else "") + body


def _strip_code(text: str) -> str:
    """Remove fenced code and inline code so `#tag` detection ignores comments."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text)


def parse(path: Path, vault: Path) -> Note:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    body = raw
    m = FENCE.match(raw)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            meta = {"_frontmatter_error": True}
        body = raw[m.end():]
    rel = str(path.relative_to(vault)).replace("\\", "/")
    return Note(rel=rel, path=path, meta=meta or {}, body=body, mtime=path.stat().st_mtime)


def parse_text(text: str, rel: str = "untitled.md") -> Note:
    meta: dict = {}
    body = text
    m = FENCE.match(text)
    if m:
        loaded = yaml.safe_load(m.group(1))
        if isinstance(loaded, dict):
            meta = loaded
        body = text[m.end():]
    return Note(rel=rel, path=Path(rel), meta=meta, body=body)


def iter_notes(vault: Path, skip: tuple[str, ...] = (".git", ".obsidian", "assistant", ".trash")):
    for p in sorted(vault.rglob("*.md")):
        rel_parts = p.relative_to(vault).parts
        if any(part in skip for part in rel_parts):
            continue
        yield p


def as_date(value: Any) -> dt.date | None:
    """Coerce a frontmatter value to a date. Vault dates are quoted YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None
