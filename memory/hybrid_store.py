"""Hybrid memory search: SQLite + sqlite-vec + FTS5 with temporal decay.

Provides semantic + keyword search over MEMORY.md and history.jsonl, with
score fusion (0.7 vector / 0.3 BM25) and exponential temporal decay for
history entries (30-day half-life, MEMORY.md exempt).

Graceful degradation: if sqlite-vec is unavailable, falls back to FTS5-only.
If sentence-transformers is unavailable, falls back to FTS5-only substring match.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

_MEMORY_SOURCE = "memory.md"
_HISTORY_SOURCE = "history.jsonl"
_HALF_LIFE_DAYS = 30.0
_VEC_WEIGHT = 0.7
_TEXT_WEIGHT = 0.3
_VEC_TOP_N = 15
_TEXT_TOP_N = 15
_DEFAULT_TOP_K = 5
_EMBEDDING_DIM = 384


@dataclass
class SearchResult:
    source: str
    source_key: str
    content: str
    score: float


class HybridStore:
    """Hybrid memory search engine combining vector similarity + BM25 full-text.

    Manages a SQLite database at ``db_path`` with three tables:
    - ``chunks``: content storage with source tracking
    - ``chunks_vec``: vec0 virtual table for cosine-distance vector search
    - ``chunks_fts``: FTS5 virtual table for BM25 keyword search
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedding_model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._db_path = Path(db_path)
        self._embedding_model_name = embedding_model_name
        self._model: object | None = None
        self._model_failed: bool = False
        self._model_dim: int = _EMBEDDING_DIM

        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._has_vec = self._load_vec_extension()
        self._create_schema()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _load_vec_extension(self) -> bool:
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            return True
        except Exception:
            logger.warning(
                "sqlite-vec not available — hybrid search will use FTS5 only"
            )
            return False

    def _create_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_key TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                chunk_id UNINDEXED,
                tokenize = "porter unicode61"
            );
        """)
        if self._has_vec:
            try:
                self._conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                        embedding float[384]
                    );
                """)
            except Exception:
                logger.warning("Failed to create vec0 table — vector search disabled")
                self._has_vec = False
        self._conn.commit()

    # ------------------------------------------------------------------
    # Embedding model (lazy singleton)
    # ------------------------------------------------------------------

    def _ensure_model(self) -> object | None:
        if self._model is not None:
            return self._model
        if self._model_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer

            self._model = self._load_model(SentenceTransformer)
            self._model_dim = self._model.get_embedding_dimension()
            return self._model
        except Exception:
            self._model_failed = True
            self._has_vec = False
            logger.warning(
                "sentence-transformers unavailable — hybrid search will use "
                "FTS5 keyword search only"
            )
            return None

    def _load_model(self, st_cls: Any) -> object:
        """Load the embedding model, preferring the local HuggingFace cache.

        Tries ``local_files_only=True`` first so a cached model loads without
        any network probe — avoiding the blocking HEAD request to
        huggingface.co (and ``Errno 101 Network is unreachable`` where it is
        unreachable).  Falls back to a normal online load, which downloads on
        first use, only when the model is not yet cached.
        """
        try:
            return st_cls(self._embedding_model_name, local_files_only=True)
        except Exception:
            logger.info(
                "Embedding model {!r} not in local cache; downloading once",
                self._embedding_model_name,
            )
            return st_cls(self._embedding_model_name)

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        model = self._ensure_model()
        if model is None:
            return None
        embeddings = model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in embeddings]

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_memory(self, content: str) -> int:
        """Re-index all MEMORY.md lines. Deletes old memory.md chunks first."""
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            return 0

        self._conn.execute(
            "DELETE FROM chunks WHERE source = ?", (_MEMORY_SOURCE,)
        )
        self._conn.execute(
            "DELETE FROM chunks_fts WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE source = ?)",
            (_MEMORY_SOURCE,),
        )
        if self._has_vec:
            self._conn.execute(
                "DELETE FROM chunks_vec WHERE rowid IN "
                "(SELECT id FROM chunks WHERE source = ?)",
                (_MEMORY_SOURCE,),
            )

        now = time.time()
        embeddings = self._embed(lines) if self._has_vec else None
        count = 0
        for i, line in enumerate(lines):
            metadata = self._parse_memory_line(line)
            cursor = self._conn.execute(
                "INSERT INTO chunks (source, source_key, content, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (_MEMORY_SOURCE, str(i), line, now, json.dumps(metadata)),
            )
            rowid = cursor.lastrowid
            self._conn.execute(
                "INSERT INTO chunks_fts (rowid, content, chunk_id) VALUES (?, ?, ?)",
                (rowid, line, rowid),
            )
            if embeddings is not None and self._has_vec and rowid is not None:
                self._conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, json.dumps(embeddings[i])),
                )
            count += 1

        self._conn.commit()
        logger.debug(f"Indexed {count} MEMORY.md lines")
        return count

    @staticmethod
    def _parse_memory_line(line: str) -> dict:
        """Extract {type, name} from memory lines like '- [project] name: content'."""
        line_stripped = line.lstrip("- ")
        result: dict[str, str] = {}
        if line_stripped.startswith("["):
            end = line_stripped.find("]")
            if end > 0:
                result["type"] = line_stripped[1:end]
                rest = line_stripped[end + 1 :].strip()
                if ":" in rest:
                    name, _ = rest.split(":", 1)
                    result["name"] = name.strip()
        return result

    def index_history(self, entries: list[dict]) -> int:
        """Index new history.jsonl entries."""
        if not entries:
            return 0

        last_cursor = self._get_meta("last_history_cursor")
        last_cursor_int = int(last_cursor) if last_cursor else 0

        new_entries = [
            e for e in entries if e.get("cursor", 0) > last_cursor_int
        ]
        if not new_entries:
            return 0

        contents = [
            e.get("content", "")[:8000] for e in new_entries
        ]
        embeddings = self._embed(contents) if self._has_vec else None
        count = 0

        for i, entry in enumerate(new_entries):
            content = contents[i]
            cursor_val = entry.get("cursor", 0)
            timestamp_str = entry.get("timestamp", "")
            created_at = self._parse_timestamp(timestamp_str)

            metadata = {"cursor": cursor_val}
            if "session_key" in entry:
                metadata["session_key"] = entry["session_key"]

            cur = self._conn.execute(
                "INSERT INTO chunks (source, source_key, content, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (_HISTORY_SOURCE, str(cursor_val), content, created_at,
                 json.dumps(metadata)),
            )
            rowid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO chunks_fts (rowid, content, chunk_id) VALUES (?, ?, ?)",
                (rowid, content, rowid),
            )
            if embeddings is not None and self._has_vec and rowid is not None:
                self._conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, json.dumps(embeddings[i])),
                )
            count += 1

        if new_entries:
            max_cursor = max(e.get("cursor", 0) for e in new_entries)
            self._set_meta("last_history_cursor", str(max_cursor))

        self._conn.commit()
        logger.debug(f"Indexed {count} history entries")
        return count

    @staticmethod
    def _parse_timestamp(ts: str) -> float:
        """Parse '%Y-%m-%d %H:%M' → Unix timestamp. Returns now on failure."""
        if not ts:
            return time.time()
        try:
            from datetime import datetime
            return datetime.strptime(ts, "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            return time.time()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = _DEFAULT_TOP_K) -> list[SearchResult]:
        """Hybrid search: vector similarity + BM25 FTS5, fused with temporal decay."""
        vec_results: dict[int, float] = {}
        text_results: dict[int, float] = {}

        if self._has_vec and self._ensure_model() is not None:
            vec_results = self._vector_search(query, _VEC_TOP_N)

        text_results = self._text_search(query, _TEXT_TOP_N)

        mode_parts: list[str] = []
        if vec_results:
            mode_parts.append(f"vector({len(vec_results)})")
        if text_results:
            mode_parts.append(f"fts5({len(text_results)})")
        logger.info("hybrid search [%s] → %d fused → %d results",
                      "+".join(mode_parts) or "none",
                      len(vec_results | text_results),
                      min(top_k, len(set(vec_results) | set(text_results))))

        fused = self._fuse(vec_results, text_results)
        fused = self._apply_temporal_decay(fused)
        fused.sort(key=lambda x: x[1], reverse=True)

        results: list[SearchResult] = []
        for chunk_id, score in fused[:top_k]:
            row = self._conn.execute(
                "SELECT source, source_key, content FROM chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if row:
                results.append(SearchResult(
                    source=row[0],
                    source_key=row[1],
                    content=row[2],
                    score=round(score, 4),
                ))
        return results

    def _vector_search(self, query: str, limit: int) -> dict[int, float]:
        query_embeddings = self._embed([query])
        if query_embeddings is None:
            return {}
        try:
            rows = self._conn.execute(
                "SELECT rowid, distance FROM chunks_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (json.dumps(query_embeddings[0]), limit),
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception:
            return {}

    def _text_search(self, query: str, limit: int) -> dict[int, float]:
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return {}
        try:
            rows = self._conn.execute(
                "SELECT chunk_id, rank FROM chunks_fts "
                "WHERE content MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit),
            ).fetchall()
            return {int(row[0]): -float(row[1]) for row in rows}
        except Exception:
            return {}

    @staticmethod
    def _build_fts_query(query: str) -> str:
        """Build a safe FTS5 query from user input."""
        tokens = query.strip().split()
        if not tokens:
            return ""
        quoted = [f'"{t}"' for t in tokens]
        return " AND ".join(quoted)

    def _fuse(
        self,
        vec_results: dict[int, float],
        text_results: dict[int, float],
    ) -> list[tuple[int, float]]:
        """Fuse vector and text scores with 0.7/0.3 weights."""
        all_ids = set(vec_results) | set(text_results)
        fused: list[tuple[int, float]] = []

        for chunk_id in all_ids:
            vec_score = 0.0
            text_score = 0.0

            if chunk_id in vec_results:
                distance = vec_results[chunk_id]
                vec_score = max(0.0, 1.0 - distance / 2.0)

            if chunk_id in text_results:
                rank = -text_results[chunk_id]
                text_score = 1.0 / (1.0 + math.exp(rank / 100.0))

            final = _VEC_WEIGHT * vec_score + _TEXT_WEIGHT * text_score
            fused.append((chunk_id, final))

        return fused

    def _apply_temporal_decay(
        self, items: list[tuple[int, float]]
    ) -> list[tuple[int, float]]:
        """Apply exponential decay to history.jsonl entries. MEMORY.md is exempt."""
        decay_lambda = math.log(2) / _HALF_LIFE_DAYS
        now = time.time()
        result: list[tuple[int, float]] = []

        for chunk_id, score in items:
            row = self._conn.execute(
                "SELECT source, created_at FROM chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if row is None:
                result.append((chunk_id, score))
                continue

            source, created_at = row[0], row[1]
            if source == _MEMORY_SOURCE:
                result.append((chunk_id, score))
            else:
                age_days = (now - created_at) / 86400.0
                decay = math.exp(-decay_lambda * max(age_days, 0))
                result.append((chunk_id, score * decay))

        return result

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, source: str, source_key: str | None = None) -> int:
        """Delete chunks by source and optional source_key. Returns count deleted."""
        if source_key is not None:
            rows = self._conn.execute(
                "SELECT id FROM chunks WHERE source = ? AND source_key = ?",
                (source, source_key),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id FROM chunks WHERE source = ?", (source,)
            ).fetchall()

        chunk_ids = [r[0] for r in rows]
        if not chunk_ids:
            return 0

        for cid in chunk_ids:
            self._conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
            if self._has_vec:
                self._conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
            self._conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
        self._conn.commit()
        return len(chunk_ids)

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> HybridStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
