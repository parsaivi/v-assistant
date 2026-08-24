"""Semantic retrieval over the vault. Local embeddings, numpy, no vector DB.

5,321 chunks × 1024 dims is a ~21MB float32 array. Brute-force cosine over that
is a single matmul — sub-millisecond. Chroma, Qdrant and pgvector would all be
strictly more infrastructure for strictly less speed at this size.

Hybrid by default: dense cosine fused with the SQLite FTS5 keyword score via
reciprocal rank fusion, because exact identifiers (ISSUE-4312, a person's name,
a Persian term the embedder has never seen) are what pure dense retrieval is
worst at.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import embed, index
from .config import Settings, settings as default_settings


@dataclass
class Hit:
    rel: str
    ord: int
    heading: str
    text: str
    score: float
    source: str = "dense"

    @property
    def link(self) -> str:
        stem = self.rel[:-3] if self.rel.endswith(".md") else self.rel
        anchor = f"#{self.heading}" if self.heading else ""
        return f"[[{stem}{anchor}]]"

    def preview(self, width: int = 220) -> str:
        t = " ".join(self.text.split())
        return t[:width] + ("…" if len(t) > width else "")


@dataclass
class Retrieval:
    query: str
    hits: list[Hit] = field(default_factory=list)
    backend: str = ""
    ms: float = 0.0
    cost: float = 0.0
    stale: bool = False   # embeddings no longer line up with the chunk table

    def notes(self) -> list[str]:
        seen, out = set(), []
        for h in self.hits:
            if h.rel not in seen:
                seen.add(h.rel)
                out.append(h.rel)
        return out

    def citations(self) -> list[str]:
        seen, out = set(), []
        for h in self.hits:
            stem = h.rel[:-3]
            if stem not in seen:
                seen.add(stem)
                out.append(f"[[{stem}]]")
        return out

    def as_context(self, max_chars: int = 6000) -> str:
        """Chunks formatted for a model prompt — used only if a paid tier is added."""
        parts, total = [], 0
        for h in self.hits:
            block = f"### {h.rel}" + (f" › {h.heading}" if h.heading else "") + \
                    f"\n{h.text.strip()}\n"
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)


class Rag:
    def __init__(self, settings: Settings | None = None,
                 conn: sqlite3.Connection | None = None,
                 backend: embed.Backend | None = None):
        self.s = settings or default_settings
        self.conn = conn or index.connect(self.s)
        self._backend = backend
        self._vectors: np.ndarray | None = None
        self._ids: list[int] = []
        self._meta: dict[int, tuple[str, int, str]] = {}
        self._loaded = False

    # ---- backend ----------------------------------------------------
    @property
    def backend(self) -> embed.Backend:
        if self._backend is None:
            self._backend = embed.build(self.s)
        return self._backend

    # ---- building ---------------------------------------------------
    def build(self, *, progress=None) -> dict:
        """Embed every chunk and persist the index. Minutes on CPU, then free."""
        t0 = time.time()
        rows = self.conn.execute(
            "SELECT id, rel, ord, heading, text FROM chunks ORDER BY id").fetchall()
        if not rows:
            raise RuntimeError("no chunks — run the vault indexer first")

        texts = [_chunk_text(r) for r in rows]
        backend = self.backend

        if isinstance(backend, embed.TfidfBackend):
            backend.fit(texts)

        vecs = np.zeros((len(texts), backend.dim), dtype=np.float32)
        batch = 64 if backend.name == "tfidf" else 16
        for i in range(0, len(texts), batch):
            vecs[i: i + batch] = backend.encode(texts[i: i + batch])
            if progress:
                progress(min(i + batch, len(texts)), len(texts))

        payload = {
            "vectors": vecs,
            "ids": np.array([r["id"] for r in rows], dtype=np.int64),
            "rels": np.array([r["rel"] for r in rows]),
            "ords": np.array([r["ord"] for r in rows], dtype=np.int32),
            "headings": np.array([r["heading"] or "" for r in rows]),
            "backend": np.array([backend.name]),
            "model": np.array([getattr(backend, "model_name", "")]),
        }
        if isinstance(backend, embed.TfidfBackend):
            payload.update({f"tfidf_{k}": v for k, v in backend.state().items()})

        self.s.index_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.s.index_path, **payload)
        self._loaded = False

        return {
            "chunks": len(rows),
            "dim": backend.dim,
            "backend": backend.name,
            "model": getattr(backend, "model_name", "—"),
            "seconds": round(time.time() - t0, 1),
            "bytes": self.s.index_path.stat().st_size,
        }

    # ---- loading ----------------------------------------------------
    def load(self) -> bool:
        if self._loaded:
            return True
        if not self.s.index_path.exists():
            return False
        data = np.load(self.s.index_path, allow_pickle=False)
        self._vectors = data["vectors"]
        rels, ords, heads = data["rels"], data["ords"], data["headings"]
        self._ids = [int(i) for i in data["ids"]]
        self._meta = {i: (str(rels[k]), int(ords[k]), str(heads[k]))
                      for k, i in enumerate(self._ids)}

        name = str(data["backend"][0])
        if self._backend is None:
            if name == "tfidf":
                state = {k[6:]: data[k] for k in data.files if k.startswith("tfidf_")}
                self._backend = embed.TfidfBackend.restore(state)
            else:
                model = str(data["model"][0])
                try:
                    self._backend = embed.SentenceTransformerBackend(
                        model, cache=self.s.state_dir.parent / "models")
                except Exception:
                    return False
        self._loaded = True
        return True

    @property
    def ready(self) -> bool:
        return self.s.index_path.exists()

    def encoder(self):
        """The *fitted* backend, for callers that want to embed short strings.

        Not the same as `.backend`: that builds a fresh one, and a fresh
        TfidfBackend has no vocabulary, so every vector it produces is zero.
        This returns the backend restored from the saved index, which is the
        only one whose output is comparable to anything. None when no index
        has been built yet — callers must degrade rather than trust zeros.
        """
        return self._backend if self.load() else None

    def info(self) -> dict:
        if not self.s.index_path.exists():
            return {"built": False}
        data = np.load(self.s.index_path, allow_pickle=False)
        return {
            "built": True,
            "chunks": int(data["vectors"].shape[0]),
            "dim": int(data["vectors"].shape[1]),
            "backend": str(data["backend"][0]),
            "model": str(data["model"][0]) or "—",
            "bytes": self.s.index_path.stat().st_size,
            "path": str(self.s.index_path),
        }

    # ---- searching --------------------------------------------------
    def search(self, query: str, k: int | None = None, *, hybrid: bool = True,
               folder: str | None = None) -> Retrieval:
        t0 = time.time()
        k = k or self.s.rag_top_k
        out = Retrieval(query=query)

        if not self.load():
            out.backend = "unavailable"
            return out
        out.backend = self.backend.name

        qv = self.backend.encode([query], query=True)[0]
        sims = self._vectors @ qv

        pool = min(len(sims), max(k * 5, 50))
        top = np.argpartition(-sims, pool - 1)[:pool]
        top = top[np.argsort(-sims[top])]
        dense = [(self._ids[i], float(sims[i])) for i in top]

        ranked = dense
        if hybrid:
            sparse = self._keyword_chunks(query, pool)
            if sparse:
                ranked = _rrf(dense, sparse, k=60)

        hits: list[Hit] = []
        misses = 0
        for cid, score in ranked:
            # Read identity from the DB, not from the saved sidecar: if the two
            # ever disagree the DB is right, and a mismatch means a stale index.
            row = self.conn.execute(
                "SELECT rel, ord, heading, text FROM chunks WHERE id=?",
                (cid,)).fetchone()
            if not row:
                misses += 1
                continue
            rel = row["rel"]
            if folder and not rel.startswith(folder):
                continue
            hits.append(Hit(rel, row["ord"], row["heading"] or "", row["text"],
                            round(score, 4), "hybrid" if hybrid else "dense"))
            if len(hits) >= k:
                break

        if misses and misses >= len(ranked) // 2:
            out.stale = True

        out.hits = hits
        out.ms = (time.time() - t0) * 1000
        index.touch(self.conn, out.notes())
        return out

    def _keyword_chunks(self, query: str, limit: int) -> list[tuple[int, float]]:
        """FTS5 over notes, mapped down to their chunks — the sparse half."""
        if not index.has_fts(self.conn):
            return []
        words = [w for w in embed.normalize(query).split() if len(w) > 1]
        if not words:
            return []
        try:
            rows = self.conn.execute(
                "SELECT f.rel, bm25(notes_fts) AS s FROM notes_fts f"
                " WHERE notes_fts MATCH ? ORDER BY s LIMIT ?",
                (" OR ".join(f'"{w}"' for w in words), max(20, limit // 3)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[int, float]] = []
        for r in rows:
            for c in self.conn.execute(
                    "SELECT id FROM chunks WHERE rel=? ORDER BY ord LIMIT 4",
                    (r["rel"],)):
                out.append((int(c["id"]), -float(r["s"])))
        return out

    # ---- neighbours (dedupe + serendipity) ---------------------------
    def similar_to_text(self, text: str, k: int = 5,
                        exclude_rel: str | None = None) -> Retrieval:
        """'Have I written about this before?' — runs on every capture, free."""
        res = self.search(text, k=k + 4)
        if exclude_rel:
            res.hits = [h for h in res.hits if h.rel != exclude_rel]
        res.hits = res.hits[:k]
        return res

    def collision(self, recent_days: int = 14, min_age_days: int = 120,
                  seed: int | None = None) -> tuple[str, str, float] | None:
        """A recent note paired with a semantically near, long-untouched one.

        The 'creative collision' from agent-second-brain: the assistant's only
        genuinely serendipitous move, and it costs nothing.
        """
        import random

        if not self.load():
            return None
        now = time.time()
        recent = [r["rel"] for r in self.conn.execute(
            "SELECT rel FROM notes WHERE mtime > ? ORDER BY mtime DESC LIMIT 40",
            (now - recent_days * 86400,))]
        if not recent:
            return None
        rng = random.Random(seed)
        anchor = rng.choice(recent)
        row = self.conn.execute("SELECT body FROM notes WHERE rel=?",
                                (anchor,)).fetchone()
        if not row or len(row["body"].split()) < 20:
            return None

        old_cutoff = now - min_age_days * 86400
        old = {r["rel"] for r in self.conn.execute(
            "SELECT rel FROM notes WHERE mtime < ?", (old_cutoff,))}
        if not old:
            return None

        res = self.search(row["body"][:1500], k=40)
        for h in res.hits:
            if h.rel in old and h.rel != anchor and h.score > 0.25:
                return anchor, h.rel, h.score
        return None


def _chunk_text(row: sqlite3.Row) -> str:
    """Prepend the note name and heading so a chunk carries its own context."""
    name = row["rel"].split("/")[-1][:-3]
    head = f" › {row['heading']}" if row["heading"] else ""
    return f"{name}{head}\n{row['text']}"


def _rrf(*rankings: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal rank fusion — combines rankings without score calibration."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])
