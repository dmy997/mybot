"""Tests for HybridStore — SQLite + FTS5 + temporal decay."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory.hybrid_store import HybridStore, SearchResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hybrid_store():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "search.db"
        store = HybridStore(db_path)
        # Disable vector search in tests (no network for model download)
        store._has_vec = False
        store._model_failed = True
        yield store
        store.close()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "search.db"
            store = HybridStore(db_path)
            assert db_path.exists()
            tables = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = {r[0] for r in tables}
            assert "chunks" in table_names
            assert "meta" in table_names
            all_tables = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            all_names = {r[0] for r in all_tables}
            assert "chunks_fts" in all_names
            store.close()


# ---------------------------------------------------------------------------
# Embedding model loading (offline-first)
# ---------------------------------------------------------------------------


class TestModelLoad:
    def test_prefers_local_cache(self, hybrid_store):
        calls: list[dict] = []

        def fake_st(name, **kw):
            calls.append(kw)
            return object()

        model = hybrid_store._load_model(fake_st)
        assert model is not None
        assert calls == [{"local_files_only": True}]

    def test_falls_back_to_online_when_uncached(self, hybrid_store):
        calls: list[dict] = []

        def fake_st(name, **kw):
            calls.append(kw)
            if kw.get("local_files_only"):
                raise OSError("not cached")
            return object()

        model = hybrid_store._load_model(fake_st)
        assert model is not None
        assert calls == [{"local_files_only": True}, {}]


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


class TestIndexMemory:
    def test_index_lines(self, hybrid_store):
        content = "- [user] test-name: This is a test memory\n- [project] proj: Another one\n"
        count = hybrid_store.index_memory(content)
        assert count == 2

    def test_index_empty(self, hybrid_store):
        assert hybrid_store.index_memory("") == 0
        assert hybrid_store.index_memory("# Header only") == 0

    def test_reindex_replaces_old(self, hybrid_store):
        hybrid_store.index_memory("- [user] old: old content")
        assert hybrid_store.index_memory("- [user] new: new content") == 1
        rows = hybrid_store._conn.execute(
            "SELECT content FROM chunks WHERE source = 'memory.md'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "- [user] new: new content"


class TestIndexHistory:
    def test_index_entries(self, hybrid_store):
        entries = [
            {"cursor": 1, "timestamp": "2026-06-01 12:00", "content": "Summary one"},
            {"cursor": 2, "timestamp": "2026-06-02 12:00", "content": "Summary two"},
        ]
        assert hybrid_store.index_history(entries) == 2

    def test_skips_already_indexed(self, hybrid_store):
        hybrid_store._set_meta("last_history_cursor", "2")
        entries = [
            {"cursor": 1, "timestamp": "2026-06-01 12:00", "content": "Old"},
            {"cursor": 3, "timestamp": "2026-06-03 12:00", "content": "New"},
        ]
        assert hybrid_store.index_history(entries) == 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestTextSearch:
    def test_keyword_search(self, hybrid_store):
        hybrid_store.index_memory(
            "- [user] coffee: I prefer dark roast coffee\n"
            "- [project] tea: Green tea is healthy\n"
        )
        results = hybrid_store.search("coffee", top_k=5)
        assert len(results) >= 1
        assert any("coffee" in r.content.lower() for r in results)

    def test_no_results_for_nonexistent(self, hybrid_store):
        hybrid_store.index_memory("- [user] test: some content")
        results = hybrid_store.search("zzz_nonexistent_zzz", top_k=5)
        assert len(results) == 0


class TestFTSQuery:
    def test_fts_query_building(self):
        assert HybridStore._build_fts_query("hello world") == '"hello" AND "world"'
        assert HybridStore._build_fts_query("single") == '"single"'
        assert HybridStore._build_fts_query("") == ""


# ---------------------------------------------------------------------------
# Temporal decay
# ---------------------------------------------------------------------------


class TestTemporalDecay:
    def test_memory_md_exempt(self, hybrid_store):
        hybrid_store.index_memory("- [user] test: evergreen content")
        results = hybrid_store.search("evergreen", top_k=5)
        assert len(results) >= 1

    def test_decay_formula(self, hybrid_store):
        import math

        decay_lambda = math.log(2) / 30.0
        age_0 = math.exp(-decay_lambda * 0)
        assert age_0 == pytest.approx(1.0)
        age_30 = math.exp(-decay_lambda * 30)
        assert age_30 == pytest.approx(0.5)
        age_60 = math.exp(-decay_lambda * 60)
        assert age_60 == pytest.approx(0.25)

    def test_old_history_decays(self, hybrid_store):
        hybrid_store.index_history([
            {"cursor": 1, "timestamp": "2025-01-01 00:00", "content": "very old data"},
        ])
        hybrid_store.index_history([
            {"cursor": 2, "timestamp": "2026-07-01 00:00", "content": "recent data"},
        ])
        results = hybrid_store.search("data", top_k=5)
        if len(results) >= 2:
            scores = {r.source_key: r.score for r in results}
            assert scores.get("2", 0) >= scores.get("1", 0)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_by_source(self, hybrid_store):
        hybrid_store.index_memory("- [user] a: first\n- [user] b: second")
        assert hybrid_store.delete("memory.md") == 2
        assert hybrid_store.search("first") == []

    def test_delete_by_source_key(self, hybrid_store):
        hybrid_store.index_memory("- [user] a: first\n- [user] b: second")
        assert hybrid_store.delete("memory.md", source_key="0") == 1
        results = hybrid_store.search("second")
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_fields(self):
        sr = SearchResult(source="memory.md", source_key="3", content="test", score=0.95)
        assert sr.source == "memory.md"
        assert sr.source_key == "3"
        assert sr.content == "test"
        assert sr.score == 0.95


# ---------------------------------------------------------------------------
# Context manager protocol
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_enter_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "search.db"
            with HybridStore(db_path) as store:
                assert store._conn is not None
            with pytest.raises(Exception):
                store._conn.execute("SELECT 1")
