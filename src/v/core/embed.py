"""Embedding backends. All local, all free.

Two backends, chosen automatically:

  sentence-transformers  bge-m3 or multilingual-e5 — real semantic search, and
                         crucially multilingual, because ~17% of this vault is
                         Persian and an English-only embedder fails on it badly.

  tfidf                  A pure-numpy character-ngram TF-IDF fallback with no
                         model download at all. Character ngrams are the reason
                         this still works on Persian: they sidestep tokenisation
                         and morphology entirely.

The fallback is not a toy. It is worse than bge-m3 at paraphrase, but it is
instant, needs no 2GB download, and beats keyword search on misspellings and
inflections. The system is fully functional before any model is fetched.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from .config import Settings, settings as default_settings

_WORD = re.compile(r"[\w؀-ۿ]+", re.UNICODE)


def normalize(text: str) -> str:
    """Fold Arabic/Persian orthographic variants so ي/ی and ك/ک match."""
    text = unicodedata.normalize("NFKC", text)
    return (text.replace("ي", "ی")   # Arabic yeh  -> Persian yeh
                .replace("ك", "ک")   # Arabic kaf  -> Persian keheh
                .replace("‌", " ")        # ZWNJ (نیم‌فاصله) -> space
                .replace("ـ", "")         # tatweel
                .lower())


class Backend(ABC):
    name: str
    dim: int

    @abstractmethod
    def encode(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        ...

    @staticmethod
    def _unit(mat: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)


class TfidfBackend(Backend):
    """Hashed char-ngram TF-IDF. No download, no model, no network."""

    name = "tfidf"

    def __init__(self, dim: int = 2048, ngrams: tuple[int, ...] = (3, 4, 5)):
        self.dim = dim
        self.ngrams = ngrams
        self.idf: np.ndarray | None = None

    def _features(self, text: str) -> dict[int, float]:
        text = normalize(text)
        words = _WORD.findall(text)
        counts: dict[int, float] = {}
        # whole words carry the most signal
        for w in words:
            h = int(hashlib.blake2b(w.encode(), digest_size=8).hexdigest(), 16) % self.dim
            counts[h] = counts.get(h, 0.0) + 1.0
        # char ngrams inside padded words handle inflection and typos
        for w in words:
            padded = f" {w} "
            for n in self.ngrams:
                for i in range(len(padded) - n + 1):
                    g = padded[i: i + n]
                    h = int(hashlib.blake2b(g.encode(), digest_size=8).hexdigest(),
                            16) % self.dim
                    counts[h] = counts.get(h, 0.0) + 0.5
        return counts

    def _raw_matrix(self, texts: list[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for h, c in self._features(t).items():
                mat[i, h] = 1.0 + math.log(c)
        return mat

    def fit(self, texts: list[str]) -> None:
        mat = self._raw_matrix(texts)
        df = (mat > 0).sum(axis=0).astype(np.float32)
        self.idf = np.log((1 + len(texts)) / (1 + df)) + 1.0

    def encode(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        mat = self._raw_matrix(texts)
        if self.idf is not None:
            mat *= self.idf
        return self._unit(mat)

    def state(self) -> dict:
        return {"idf": self.idf if self.idf is not None else np.zeros(self.dim,
                                                                     dtype=np.float32),
                "dim": np.array([self.dim]),
                "ngrams": np.array(self.ngrams)}

    @classmethod
    def restore(cls, state) -> "TfidfBackend":
        b = cls(dim=int(state["dim"][0]), ngrams=tuple(int(n) for n in state["ngrams"]))
        idf = state["idf"]
        b.idf = idf if idf.any() else None
        return b


class SentenceTransformerBackend(Backend):
    """bge-m3 / multilingual-e5. Downloads once, then runs offline on CPU."""

    name = "sentence-transformers"

    def __init__(self, model_name: str, cache: Path | None = None):
        # The HF cache location is set in config.py at import time — doing it
        # here would be too late (see the comment there).
        if cache:
            cache.mkdir(parents=True, exist_ok=True)

        from sentence_transformers import SentenceTransformer  # lazy

        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())
        # e5 models require these prefixes; bge-m3 ignores them harmlessly.
        self.needs_prefix = "e5" in model_name.lower()

    def encode(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        if self.needs_prefix:
            prefix = "query: " if query else "passage: "
            texts = [prefix + t for t in texts]
        vecs = self.model.encode(
            texts, batch_size=8, convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=False,
        )
        return vecs.astype(np.float32)


def build(settings: Settings | None = None, *, prefer: str | None = None) -> Backend:
    """Pick the best available backend without ever failing to return one."""
    s = settings or default_settings
    choice = prefer or s.embedding_backend

    if choice in ("auto", "sentence-transformers"):
        from .config import MODEL_DIR

        problems: list[str] = []
        for name in (s.embedding_model, s.embedding_fallback_model):
            try:
                return SentenceTransformerBackend(name, cache=MODEL_DIR)
            except Exception as exc:
                # Never swallow the reason — a silent fallback to tf-idf looks
                # like the system working, just worse, which is hard to notice.
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
        if choice == "sentence-transformers":
            raise RuntimeError(
                "sentence-transformers backend requested but no model loaded:\n  "
                + "\n  ".join(problems)
            )
        _LAST_FALLBACK_REASON.clear()
        _LAST_FALLBACK_REASON.extend(problems)
    return TfidfBackend()


_LAST_FALLBACK_REASON: list[str] = []


def fallback_reason() -> list[str]:
    """Why the good backend wasn't used, if it wasn't. Surfaced in `/status`."""
    return list(_LAST_FALLBACK_REASON)
