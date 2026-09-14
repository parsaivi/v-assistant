"""media.py — the pictures a turn is about, found while it is still running.

Every other surface V has is words. The HUD is the one with room for a
picture, and there are already pictures: `render.py` writes charts, maps and
route PNGs into `state_dir/renders/`, and the vault holds 112 screenshots
pasted into notes. Neither has ever reached a screen V controls — a render is
`webbrowser.open()`ed into whatever tab happens to steal focus, and a
screenshot in a note is invisible unless Obsidian is the window in front.

This module is the finder. `hud.py` is the frame it goes in.

**Two channels, because there are two kinds of picture.**

*Deliberate* ones — the chart V just drew — arrive through the spool:
`render.save()` and `render.save_bytes()` are the choke point every artifact
in this codebase passes through, so announcing there means a render invented
next year shows up on the HUD without anybody wiring it. The spool is a
JSONL file rather than the outbox in `files.py`, and the difference is the
whole reason it exists: the outbox is a *delivery queue*, claimed once, and a
HUD draining it would silently eat the chart the phone was waiting for. A
frame is a *view* — the same PNG can be looked at on every screen that is
open, and showing it twice costs nothing.

*Ambient* ones — the screenshot in the note V is reading right now — are
found from `shells.ShellEvent.rel`/`.rels`, which have carried candidate note
paths since the day the stream parser was written and which no surface has
ever drawn. That is the "in parallel with the answer" half: the paths arrive
while the model is still writing, so the picture is on screen before the
sentence about it is.

**Nothing here is private-filtered, deliberately.** `mcp/guard.py` keeps
private notes out of a *model's* context; it was never about keeping them off
Parsa's own screen, and the HUD already renders private notes in its VAULT
search and its drift list. A screenshot pasted into `12 - Brain Dump/` is his,
on his machine, on a loopback port. The rule that matters is the one in
`hud.py`: nothing on that page ever leaves it.

**Nothing here blocks a turn.** Every function is called from `hud._Scout`'s
worker thread. `place_map()` in particular is a network round trip to Neshan
and is cached on disk, because the alternative is a tool call's worth of
latency landing on the progress callback of the thread running the answer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, settings as default_settings

#: What the viewport can draw as a picture, and what it has to frame as a
#: page instead. Deliberately narrower than `files._MIME`: a PDF and a video
#: are things to open, not things to glance at, and a viewport that pops one
#: open mid-answer is a viewport nobody leaves on.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
PAGE_SUFFIXES = (".html", ".htm")

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
        ".svg": "image/svg+xml", ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8"}

#: How many embeds one note may contribute. A bug-report note in `15 - DUNG/`
#: carries eight screenshots of the same screen; the first few are the
#: evidence and the rest are the scroll. The note path is on the frame, so
#: the ones cut are one click away in Obsidian rather than lost.
N_PER_NOTE = 4

#: `attachments/` is rebuilt this often at most. It is one `rglob` over a
#: directory of 112 files — nothing, but not nothing on every tool call.
ATTACH_TTL = 30.0


@dataclass
class Item:
    """One picture, with where it came from still attached.

    `source` is the provenance the viewport prints under the image, and it is
    not decoration: a chart V drew from vault numbers and a screenshot Parsa
    pasted into a note two months ago look identical once they are both PNGs
    on a black panel, and only one of them is evidence of anything.
    """

    path: Path
    kind: str = "image"           # image · page
    title: str = ""
    caption: str = ""
    source: str = "note"          # note · render · place
    rel: str = ""                 # the vault note it came from, if any
    at: float = field(default_factory=time.time)

    @property
    def bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def kind_for(path: Path | str) -> str:
    """`image`, `page`, or "" for something the viewport should not open."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in PAGE_SUFFIXES:
        return "page"
    return ""


def mime_for(path: Path | str) -> str:
    return MIME.get(Path(path).suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------
# the spool — artifacts, announced by whatever process rendered them

def spool_path(s: Settings) -> Path:
    return s.state_dir / "renders" / "spool.jsonl"


#: Rotated at this size, keeping the tail. ~2,000 artifacts, so in practice
#: never; the cap exists because this file is appended to by a subprocess
#: nobody supervises and an unbounded log in `state_dir` is a bug waiting for
#: a quiet year to happen in.
_SPOOL_MAX = 512 * 1024
_SPOOL_KEEP = 200


def announce(path: Path, *, kind: str = "", title: str = "", caption: str = "",
             source: str = "render", settings: Settings | None = None) -> None:
    """Say that an artifact exists, to whatever surface is open.

    Best-effort by contract: a chart that renders and then fails to announce
    itself is still a chart in the browser tab `open_file()` opened, and a
    write error here must never turn into a failed tool call. `render.save()`
    calls this and does not check.
    """
    s = settings or default_settings
    kind = kind or kind_for(path)
    if not kind:
        return
    line = json.dumps({"at": time.time(), "path": str(path), "kind": kind,
                       "title": title or path.stem, "caption": caption,
                       "source": source}, ensure_ascii=False)
    try:
        out = spool_path(s)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size > _SPOOL_MAX:
            tail = out.read_text(encoding="utf-8").splitlines()[-_SPOOL_KEEP:]
            out.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with out.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class Spool:
    """The reading half. Holds a byte offset, hands back what is new.

    Opens at the *end* of the file, not the start: frames are a live view of
    what V is doing, not a history of every chart it has ever drawn, and a
    HUD started at noon should not open onto this morning's renders.
    """

    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings
        self.at = 0
        try:
            self.at = spool_path(self.s).stat().st_size
        except OSError:
            self.at = 0

    def take(self) -> list[Item]:
        path = spool_path(self.s)
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < self.at:
            # Rotated under us. Seeking to 0 would replay the kept tail as if
            # it had all just happened; the honest loss is the handful of
            # lines inside the rotation window.
            self.at = size
            return []
        if size == self.at:
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.at)
                raw = fh.read()
                self.at = path.stat().st_size
        except OSError:
            return []
        out: list[Item] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue            # a half-written line; the next read gets it
            p = Path(str(row.get("path") or ""))
            if not p.is_file():
                continue            # announced and then deleted
            out.append(Item(path=p, kind=str(row.get("kind") or kind_for(p)),
                            title=str(row.get("title") or p.stem),
                            caption=str(row.get("caption") or ""),
                            source=str(row.get("source") or "render"),
                            at=float(row.get("at") or time.time())))
        return out


# --------------------------------------------------------------------------
# embeds — the pictures already inside the vault

#: `![[Pasted image 20260717030102.png]]`, with an optional `|size` alias.
#: Non-greedy and newline-stopped for `shells._VAULT_REL`'s reason: a `]]`
#: inside a Dataview block is not the end of a link.
EMBED_WIKI = re.compile(r"!\[\[([^\]\n|#]+?)(?:\|[^\]\n]*)?\]\]")
#: `![alt](attachments/x.png)` — the standard-markdown spelling. Obsidian
#: writes the wikilink form, but notes imported from elsewhere use this one.
EMBED_MD = re.compile(r"!\[([^\]\n]*)\]\(([^)\n\s]+)(?:\s+\"[^\"]*\")?\)")


def _attachments(s: Settings, cache: dict) -> dict[str, Path]:
    """Every image in the vault, by filename.

    Obsidian resolves `![[Pasted image 2026….png]]` by *basename*, anywhere in
    the vault, with no path — so a resolver that only tried the literal string
    as a path would find nothing at all in this vault, where every embed is
    written that way and every file lives in `attachments/`.
    """
    now = time.time()
    if cache.get("at", 0) + ATTACH_TTL > now and "by_name" in cache:
        return cache["by_name"]
    by_name: dict[str, Path] = {}
    try:
        for p in s.vault.rglob("*"):
            if p.suffix.lower() not in IMAGE_SUFFIXES or not p.is_file():
                continue
            if ".git" in p.parts or ".obsidian" in p.parts:
                continue
            by_name.setdefault(p.name.lower(), p)
    except OSError:
        pass
    cache["by_name"], cache["at"] = by_name, now
    return by_name


def resolve_embed(s: Settings, target: str, *, from_rel: str = "",
                  cache: dict | None = None) -> Path | None:
    """An embed target to a file on disk, or None.

    Three tries, in the order Obsidian itself resolves them: vault-relative,
    relative to the embedding note, then by bare filename. The last one is
    the one that actually fires here — see `_attachments`.
    """
    target = (target or "").split("#", 1)[0].strip()
    if not target or target.startswith(("http://", "https://", "data:")):
        return None
    if kind_for(target) != "image":
        return None
    root = s.vault.resolve()
    for candidate in (root / target,
                      (root / from_rel).parent / target if from_rel else None):
        if candidate is None:
            continue
        try:
            p = candidate.resolve()
        except OSError:
            continue
        # A note is vault prose and `../../` in an embed is not a path this
        # surface follows out of the vault. `is_relative_to` rather than a
        # string prefix: a sibling directory called `Brain-old` starts with
        # the vault's own path and is not inside it.
        if p.is_file() and p.is_relative_to(root):
            return p
    hit = _attachments(s, cache if cache is not None else {}).get(
        Path(target).name.lower())
    return hit if hit and hit.is_file() else None


def rel_from_citation(cite: str) -> str:
    """`[[09 - …/Locations/Kaboom|Kaboom]]` to `09 - …/Locations/Kaboom.md`.

    A citation is what a *local* answer leaves behind — `query.Row.link()`
    and `rag.Result.citations()` both build one — and it is the only trace a
    structured answer leaves at all. "Where is Kaboom?" is answered from
    SQL with no shell and therefore no `ShellEvent`, so without this the one
    question the viewport exists for would show nothing.
    """
    s = (cite or "").strip()
    if not (s.startswith("[[") and s.endswith("]]")):
        return ""
    s = s[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    if not s or s.startswith(("http://", "https://")):
        return ""
    return s if s.endswith(".md") else s + ".md"


def images_in_note(s: Settings, rel: str, *, limit: int = N_PER_NOTE,
                   cache: dict | None = None) -> list[Item]:
    """The pictures embedded in one note, in the order the note embeds them.

    Reads the file rather than the index: attachments are not indexed (the
    mirror is notes, links, tags and chunks — see `index.py`), and the body is
    one small read on a worker thread.
    """
    rel = (rel or "").strip()
    if not rel or not rel.endswith(".md"):
        return []
    path = s.vault / rel
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    note_title = Path(rel).stem
    out: list[Item] = []
    seen: set[str] = set()
    for target, alt in _embeds(body):
        found = resolve_embed(s, target, from_rel=rel, cache=cache)
        if found is None or str(found) in seen:
            continue
        seen.add(str(found))
        out.append(Item(path=found, kind="image",
                        title=alt.strip() or found.stem,
                        caption=note_title, source="note", rel=rel))
        if len(out) >= limit:
            break
    return out


def _embeds(body: str) -> list[tuple[str, str]]:
    """`(target, alt)` for every image embed, in document order.

    Both spellings in one pass and sorted by position, because a note that
    mixes them — an imported bug report with an Obsidian screenshot pasted
    underneath — would otherwise show its pictures out of order.
    """
    found: list[tuple[int, str, str]] = []
    for m in EMBED_WIKI.finditer(body):
        found.append((m.start(), m.group(1), ""))
    for m in EMBED_MD.finditer(body):
        found.append((m.start(), m.group(2), m.group(1)))
    found.sort()
    return [(target, alt) for _, target, alt in found]


# --------------------------------------------------------------------------
# places — a note with coordinates, as a map

#: Tight enough to read the street, wide enough to place it in the city.
PLACE_ZOOM = 15
PLACE_SIZE = (560, 360)


def is_place(s: Settings, rel: str) -> bool:
    return bool(rel) and rel.startswith(s.folders.locations + "/")


def place_map(s: Settings, rel: str) -> Item | None:
    """A place note as a picture of where it is, or None.

    None rather than an exception for everything: no coordinates in the note,
    no Neshan key configured, the static-map box not ticked on the key, the
    network down. This is the ambient half of the HUD — a map that could not
    be fetched is a frame that does not appear, never a fault chip in the
    header for a picture nobody asked for out loud.

    Cached on disk under `renders/places/` and keyed by coordinate, so the
    second time a café comes up in conversation it costs nothing. Not
    announced to the spool: the scout that asks for it already has it, and
    announcing would draw it twice.
    """
    from . import notes as notes_mod       # yaml, and only on this path
    from . import query as query_mod

    path = s.vault / rel
    if not path.is_file():
        return None
    try:
        note = notes_mod.parse(path, s.vault)
    except (OSError, ValueError):
        return None
    coord = query_mod._parse_latlng(note.meta.get("location"))
    if coord is None:
        return None
    lat, lng = coord
    out_dir = s.state_dir / "renders" / "places"
    slug = re.sub(r"[^a-z0-9]+", "-", Path(rel).stem.lower()).strip("-") or "place"
    dest = out_dir / f"{slug}-{lat:.5f}-{lng:.5f}-z{PLACE_ZOOM}.png"
    title = str(note.meta.get("title") or note.title)
    caption = " · ".join(x for x in (
        str(note.meta.get("neighborhood") or ""),
        str(note.meta.get("city") or ""),
        f"{lat:.4f}, {lng:.4f}") if x)
    if dest.is_file():
        return Item(path=dest, kind="image", title=title, caption=caption,
                    source="place", rel=rel)

    from . import mapsearch                # httpx, and only on this path
    try:
        blob = mapsearch.static_map(lat, lng, PLACE_ZOOM, *PLACE_SIZE,
                                    settings=s)
    except mapsearch.MapSearchError:
        return None
    if not blob:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    except OSError:
        return None
    return Item(path=dest, kind="image", title=title, caption=caption,
                source="place", rel=rel)
