"""files — what a file is to V, in both directions.

Not a transport concern, which is why it is here and not in `httpapi.py` where
it started. Three surfaces need the same answers: the phone uploads and
downloads them, the scheduler sweeps the ones nobody collected, and the MCP
server queues them from inside whatever process an agent turn happened to
spawn. A second set of rules about where a photo lives would mean two places to
look for it six months later.

The outbox is the interesting half. The process that *makes* a file is usually
not the process holding a socket to the phone — `vault_send_file` runs inside
an MCP subprocess, while the event stream lives in `v-serve` — so a row in
SQLite is the whole channel between them. Durable on purpose: a chart rendered
while the phone was in a tunnel is still waiting when it comes back, rather
than having been broadcast to nobody.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import provenance
from .config import Settings, settings as default_settings

# Where a phone's attachments land. Beside `attachments/telegram/` rather than
# mixed in with it: the folder is the provenance, and six months later "where
# did this photo come from" is answerable by looking at the path.
ATTACH_DIR = "attachments/phone"

# 25 MB. Not a network limit — this is a LAN and the phone is the only client —
# but a *vault* limit. Every attachment is a git commit in a repository that is
# cloned, backed up and carried around, and a repo that gains a 400 MB screen
# recording is one nobody can clone again. Telegram's own bot limit is 50 MB
# and this is deliberately stricter, because Telegram is not storing the result
# forever.
ATTACH_LIMIT = 25 * 1024 * 1024

# What a browser or a phone should call the bytes coming back. Kept small and
# explicit rather than reaching for `mimetypes`, which on a headless server
# reads /etc/mime.types and can disagree with itself between machines.
_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".mp4": "video/mp4",
    ".zip": "application/zip",
}

_UNSAFE = re.compile(r"[^\w\s.؀-ۿ-]")


def mime_for(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "application/octet-stream")


def safe_name(name: str) -> str:
    """A filename the vault can hold, keeping Persian letters.

    Same shape as `telegram._safe_filename` and the same reason — a name
    arrives from outside and ends up as a path — with the range for Arabic
    script kept, because half the files this vault stores are named in it.
    """
    cleaned = _UNSAFE.sub("", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80]


def save_attachment(engine, data: bytes, name: str) -> dict:
    """Put an uploaded file in the vault, and say where it went.

    Deliberately the same three moves `telegram._save_attachment` makes —
    timestamp the name, refuse to collide, `save_binary` — because they are the
    same operation arriving over a different wire, and a second set of rules
    for where phone files live is a second place to look for a photo.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = Path(name or "").suffix[:12]
    stem = safe_name(Path(name or "file").stem) or "file"
    rel = f"{ATTACH_DIR}/{stamp}-{stem}{ext}"
    n = 2
    while engine.vault.exists(rel):
        rel = f"{ATTACH_DIR}/{stamp}-{stem}-{n}{ext}"
        n += 1
    engine.vault.save_binary(rel, data, summary=f"phone attachment: {stem}{ext}",
                             source=provenance.PARSA)
    return file_meta(engine.s, rel)


def file_meta(s: Settings, rel: str, caption: str = "") -> dict:
    """What the phone needs to draw a file without downloading it first."""
    try:
        size = (s.vault / rel).stat().st_size
    except OSError:
        size = 0
    return {"rel": rel, "name": Path(rel).name, "bytes": size,
            "mime": mime_for(rel), "caption": caption}


def take_queued_files(engine) -> list[dict]:
    """Everything V has put in the outbox and nobody has collected yet.

    The outbox exists because the process that *makes* a file is usually not
    the process holding a socket to the phone. `vault_send_file` runs inside
    the MCP subprocess an agent turn spawned; the event stream lives here. A
    row in SQLite is the whole channel between them, and it is durable, which
    matters more than it sounds: a chart rendered while the phone was in a
    tunnel is still waiting when it comes back, rather than having been
    broadcast to nobody.

    `claim` rather than `mark_sent` because two drainers read this — see there.
    """
    out: list[dict] = []
    for row in engine.reminders.unsent():
        if row.get("kind") != "file":
            continue
        if not engine.reminders.claim(row["id"]):
            continue                     # the other drainer got there first
        try:
            item = json.loads(row["body"] or "{}")
        except json.JSONDecodeError:
            continue                     # claimed and dropped: it was garbage
        rel = item.get("rel")
        if not rel or not engine.vault.exists(rel):
            continue                     # deleted between queue and delivery
        out.append(file_meta(engine.s, rel, item.get("caption", "")))
    return out
