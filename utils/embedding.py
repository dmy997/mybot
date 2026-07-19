"""Shared embedding model singleton for semantic similarity.

Used by memory/hybrid_store.py and context/semantic_filter.py.
Lazy-loads sentence-transformers on first use; falls back gracefully.
"""

from __future__ import annotations

from config import Config
from loguru import logger


class EmbeddingModel:
    """Lazy-loaded sentence-transformers model shared across the process."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or Config.embedding_model
        self._model: object | None = None
        self._failed: bool = False
        self._dim: int = 384  # default for all-MiniLM-L6-v2

    @property
    def dim(self) -> int:
        return self._dim

    # -- lazy load -------------------------------------------------------

    def _ensure_model(self) -> object | None:
        if self._model is not None:
            return self._model
        if self._failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer

            self._model = self._load_model(SentenceTransformer)
            self._dim = self._model.get_embedding_dimension()
            return self._model
        except Exception:
            self._failed = True
            logger.warning(
                "sentence-transformers unavailable — semantic filtering disabled"
            )
            return None

    def _load_model(self, st_cls: type) -> object:
        try:
            return st_cls(self._model_name, local_files_only=True)
        except Exception:
            logger.info(
                "Embedding model {!r} not in local cache; downloading once",
                self._model_name,
            )
            return st_cls(self._model_name)

    # -- public API -------------------------------------------------------

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Encode *texts* to embedding vectors.  Returns None on failure."""
        model = self._ensure_model()
        if model is None:
            return None
        embeddings = model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in embeddings]

    @property
    def available(self) -> bool:
        return self._ensure_model() is not None


# -- global singleton ----------------------------------------------------

_EMBEDDING_MODEL: EmbeddingModel | None = None


def get_embedding_model() -> EmbeddingModel:
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        _EMBEDDING_MODEL = EmbeddingModel()
    return _EMBEDDING_MODEL
