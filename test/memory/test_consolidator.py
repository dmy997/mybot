"""Tests for Consolidator and history.jsonl management."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from context.session import Session
from memory.consolidator import Consolidator
from memory.store import MemoryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ws():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def store(ws):
    return MemoryStore(ws)


@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    return provider


# ---------------------------------------------------------------------------
# MemoryStore — history.jsonl
# ---------------------------------------------------------------------------


class TestHistoryStore:
    def test_append_and_read_history(self, store):
        c1 = store.append_history("User prefers dark mode")
        c2 = store.append_history("Decided to use PostgreSQL")
        assert c2 == c1 + 1
        assert c1 >= 1

        entries = store.read_history()
        assert len(entries) >= 2
        assert entries[-1]["content"] == "Decided to use PostgreSQL"

    def test_read_history_since_cursor(self, store):
        store.append_history("First entry")
        c2 = store.append_history("Second entry")
        store.append_history("Third entry")

        entries = store.read_history(since_cursor=c2)
        assert len(entries) == 1
        assert entries[0]["content"] == "Third entry"

    def test_read_history_empty(self, store):
        assert store.read_history() == []
        assert store.read_history(since_cursor=100) == []

    def test_compact_history(self, store):
        store.max_history_entries = 3
        for i in range(5):
            store.append_history(f"Entry {i}")

        removed = store.compact_history()
        assert removed == 2
        entries = store.read_history()
        assert len(entries) == 3
        assert entries[0]["content"] == "Entry 2"

    def test_compact_history_no_op(self, store):
        store.append_history("Only entry")
        removed = store.compact_history()
        assert removed == 0

    def test_cursor_persistence(self, store):
        """Cursor survives store re-creation."""
        c1 = store.append_history("Entry")
        store2 = MemoryStore(store.workspace)
        c2 = store2.append_history("Another")
        assert c2 == c1 + 1

    def test_dream_cursor(self, store):
        assert store.get_dream_cursor() == 0
        store.set_dream_cursor(42)
        assert store.get_dream_cursor() == 42

    def test_raw_archive(self, store):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        store.raw_archive(messages)
        entries = store.read_history()
        assert len(entries) == 1
        assert "[RAW]" in entries[0]["content"]
        assert "Hello" in entries[0]["content"]


# ---------------------------------------------------------------------------
# MemoryStore — MEMORY.md full-text
# ---------------------------------------------------------------------------


class TestMemoryFile:
    def test_write_and_read_memory_file(self, store):
        store.write_memory_file("# Project Notes\n\n- Item 1\n- Item 2")
        content = store.read_memory_file()
        assert "Project Notes" in content
        assert "Item 1" in content

    def test_is_template_detected(self, store):
        assert store._is_template_content("Edit this file to customize...") is True
        assert store._is_template_content("(your timezone)") is True
        assert store._is_template_content("Real memory content") is False

    def test_get_memory_context_suppresses_template(self, store):
        store.write_memory_file("Edit this file to customize your settings")
        assert store.get_memory_context() == ""

    def test_get_memory_context_returns_content(self, store):
        store.write_memory_file("# Real Knowledge\n\nImportant fact.")
        ctx = store.get_memory_context()
        assert "Real Knowledge" in ctx


# ---------------------------------------------------------------------------
# Consolidator
# ---------------------------------------------------------------------------


class TestConsolidator:
    @pytest.fixture
    def session(self):
        s = Session(key="test:123")
        s.messages = [
            {"role": "user", "content": "I prefer tabs over spaces"},
            {"role": "assistant", "content": "Got it, using tabs"},
            {"role": "user", "content": "Let's build a web app"},
            {"role": "assistant", "content": "What kind of web app?"},
        ]
        return s

    @pytest.mark.asyncio
    async def test_maybe_consolidate_idle_when_under_budget(self, store, mock_provider, session):
        """When token estimate is under budget, no consolidation happens."""
        c = Consolidator(store, mock_provider, "test-model", context_window_tokens=128_000)
        result = await c.maybe_consolidate(session, build_messages_fn=None)
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_consolidate_skips_when_no_provider(self, store, session):
        """No consolidation when provider is None."""
        c = Consolidator(store, provider=None, context_window_tokens=128_000)
        result = await c.maybe_consolidate(session, build_messages_fn=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_maybe_consolidate_skips_empty_session(self, store, mock_provider):
        session = Session(key="test:456")
        c = Consolidator(store, mock_provider, "test-model")
        result = await c.maybe_consolidate(session, build_messages_fn=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_archive_writes_to_history(self, store, mock_provider):
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="- User prefers tabs\n- Building a web app",
            finish_reason="stop",
        )
        c = Consolidator(store, mock_provider, "test-model")
        messages = [
            {"role": "user", "content": "I prefer tabs"},
            {"role": "assistant", "content": "Noted"},
        ]
        summary = await c.archive(messages)
        assert summary is not None
        assert "tabs" in summary
        entries = store.read_history()
        assert len(entries) >= 1
        assert entries[-1]["content"] == summary

    @pytest.mark.asyncio
    async def test_archive_empty_messages(self, store, mock_provider):
        c = Consolidator(store, mock_provider, "test-model")
        result = await c.archive([])
        assert result is None
        mock_provider.chat_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_archive_fallback_on_llm_error(self, store, mock_provider):
        mock_provider.chat_with_retry.side_effect = RuntimeError("LLM down")
        c = Consolidator(store, mock_provider, "test-model")
        messages = [{"role": "user", "content": "Hello"}]
        result = await c.archive(messages)
        assert result is None  # falls back to raw_archive
        entries = store.read_history()
        assert len(entries) == 1
        assert "[RAW]" in entries[0]["content"]

    @pytest.mark.asyncio
    async def test_pick_boundary_at_user_turn(self, store, mock_provider):
        c = Consolidator(store, mock_provider, "test-model")
        messages = [
            {"role": "assistant", "content": "x" * 100},
            {"role": "user", "content": "y" * 100},
            {"role": "assistant", "content": "z" * 100},
            {"role": "user", "content": "w" * 100},
        ]
        boundary = c._pick_boundary(messages, 30)  # ~30 tokens
        assert boundary is not None
        assert boundary > 0

    @pytest.mark.asyncio
    async def test_last_consolidated_advances(self, store, mock_provider):
        """After consolidation, last_consolidated advances."""
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="- Learned something new",
            finish_reason="stop",
        )
        session = Session(key="test:789")
        # Create enough messages to trigger consolidation
        big_content = "x" * 200_000  # ~50k tokens — well over budget
        session.messages = [
            {"role": "user", "content": big_content},
            {"role": "assistant", "content": "OK"},
        ]

        c = Consolidator(store, mock_provider, "test-model", context_window_tokens=10_000)
        result = await c.maybe_consolidate(session, build_messages_fn=None)
        assert result is True
        assert session.last_consolidated > 0
