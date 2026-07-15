"""Tests for Dream memory consolidation (SOUL.md, USER.md, MEMORY.md)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.dream import Dream
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


@pytest.fixture
def dream(store, mock_provider):
    return Dream(store, mock_provider, "test-model")


# ---------------------------------------------------------------------------
# run — basic scenarios
# ---------------------------------------------------------------------------


class TestDreamRun:
    @pytest.mark.asyncio
    async def test_no_provider_skips(self, store):
        d = Dream(store, provider=None)
        result = await d.run()
        assert result is False

    @pytest.mark.asyncio
    async def test_no_new_entries_skips(self, dream):
        result = await dream.run()
        assert result is False

    @pytest.mark.asyncio
    async def test_adds_to_memory(self, dream, store):
        store.append_history("- User created a new project")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[FILE] MEMORY.md: User created a new project called mybot",
            finish_reason="stop",
        )

        result = await dream.run()
        assert result is True
        assert "mybot" in store.read_memory_file()
        assert store.get_dream_cursor() == 1

    @pytest.mark.asyncio
    async def test_adds_to_soul(self, dream, store):
        store.append_history("- User asked bot to always respond in Chinese")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[FILE] SOUL.md: Always respond in Chinese",
            finish_reason="stop",
        )

        result = await dream.run()
        assert result is True
        assert "Chinese" in store.read_soul()

    @pytest.mark.asyncio
    async def test_adds_to_user(self, dream, store):
        store.append_history("- User's name is Zhang San, lives in Beijing")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[FILE] USER.md: Name is Zhang San, lives in Beijing",
            finish_reason="stop",
        )

        result = await dream.run()
        assert result is True
        assert "Zhang San" in store.read_user()

    @pytest.mark.asyncio
    async def test_multiple_files(self, dream, store):
        """One LLM response updates all three files."""
        store.append_history("- User changed jobs, now works at Alibaba")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content=(
                "[FILE] USER.md: Works at Alibaba\n"
                "[FILE-REMOVE] USER.md: Works at Google\n"
            ),
            finish_reason="stop",
        )

        store.write_user("- Name: Zhang San\n- Works at Google\n")

        result = await dream.run()
        assert result is True
        assert "Alibaba" in store.read_user()
        assert "Google" not in store.read_user()

    @pytest.mark.asyncio
    async def test_correction_pattern(self, dream, store):
        """Correction = [FILE] new fact + [FILE-REMOVE] old fact."""
        store.append_history("- User actually uses Rust, not Python as previously stated")
        store.write_memory_file("## Tech Stack\n\n- Primary language: Python\n")

        dream.provider.chat_with_retry.return_value = MagicMock(
            content=(
                "[FILE] MEMORY.md: Primary language: Rust\n"
                "[FILE-REMOVE] MEMORY.md: Primary language: Python\n"
            ),
            finish_reason="stop",
        )

        result = await dream.run()
        assert result is True
        updated = store.read_memory_file()
        assert "Rust" in updated
        assert "Python" not in updated

    @pytest.mark.asyncio
    async def test_skip_directive(self, dream, store):
        """[SKIP] means nothing to do."""
        store.append_history("- nothing important")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[SKIP]",
            finish_reason="stop",
        )

        result = await dream.run()
        assert result is False
        # Cursor still advances
        assert store.get_dream_cursor() == 1

    @pytest.mark.asyncio
    async def test_llm_error_handled(self, dream, store):
        store.append_history("- some entry")
        dream.provider.chat_with_retry.side_effect = RuntimeError("LLM down")
        result = await dream.run()
        assert result is False

    @pytest.mark.asyncio
    async def test_finish_reason_error_raises(self, dream, store):
        store.append_history("- test")
        dream.provider.chat_with_retry.return_value = MagicMock(
            content="",
            finish_reason="error",
        )
        result = await dream.run()
        assert result is False


# ---------------------------------------------------------------------------
# Batch capping
# ---------------------------------------------------------------------------


class TestBatchCap:
    @pytest.mark.asyncio
    async def test_caps_at_max_batch_size(self, dream, store):
        for i in range(25):
            store.append_history(f"- Entry {i}")

        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[FILE] MEMORY.md: Merged facts",
            finish_reason="stop",
        )

        await dream.run()
        call_args = dream.provider.chat_with_retry.call_args
        user_content = call_args[1]["messages"][1]["content"]
        assert "Entry 0" not in user_content
        assert "Entry 24" in user_content

    @pytest.mark.asyncio
    async def test_advances_cursor_to_max(self, dream, store):
        store.append_history("- First")
        store.append_history("- Second")
        store.append_history("- Third")

        dream.provider.chat_with_retry.return_value = MagicMock(
            content="[FILE] MEMORY.md: Updated",
            finish_reason="stop",
        )

        await dream.run()
        assert store.get_dream_cursor() == 3


# ---------------------------------------------------------------------------
# _parse_directives
# ---------------------------------------------------------------------------


class TestParseDirectives:
    def test_parse_file_add(self, dream):
        text = "[FILE] SOUL.md: Speak Chinese"
        adds, removes, skills = dream._parse_directives(text)
        assert adds == [("SOUL", "Speak Chinese")]
        assert removes == []
        assert skills == []

    def test_parse_file_remove(self, dream):
        text = "[FILE-REMOVE] MEMORY.md: Old database fact"
        adds, removes, skills = dream._parse_directives(text)
        assert adds == []
        assert removes == [("MEMORY", "Old database fact")]
        assert skills == []

    def test_parse_mixed(self, dream):
        text = (
            "[FILE] USER.md: Lives in Shanghai\n"
            "[FILE] MEMORY.md: Project uses Redis\n"
            "[FILE-REMOVE] USER.md: Lives in Beijing\n"
        )
        adds, removes, skills = dream._parse_directives(text)
        assert len(adds) == 2
        assert len(removes) == 1
        assert len(skills) == 0
        assert adds[0] == ("USER", "Lives in Shanghai")
        assert adds[1] == ("MEMORY", "Project uses Redis")
        assert removes[0] == ("USER", "Lives in Beijing")

    def test_parse_skip(self, dream):
        adds, removes, skills = dream._parse_directives("[SKIP]")
        assert adds == []
        assert removes == []
        assert skills == []

    def test_parse_skip_with_newlines(self, dream):
        adds, removes, skills = dream._parse_directives("[SKIP]\n")
        assert adds == []
        assert skills == []

    def test_parse_unparseable_skipped(self, dream):
        """Garbage lines are ignored without crashing."""
        adds, removes, skills = dream._parse_directives(
            "Here is what I found:\n[FILE] USER.md: Real fact\nSome random text\n"
        )
        assert len(adds) == 1
        assert adds[0] == ("USER", "Real fact")
        assert skills == []

    def test_normalize_filenames(self, dream):
        """SOUL.md / SOUL are both accepted."""
        a1, _, _ = dream._parse_directives("[FILE] SOUL.md: fact a")
        a2, _, _ = dream._parse_directives("[FILE] SOUL: fact b")
        assert a1[0][0] == "SOUL"
        assert a2[0][0] == "SOUL"

    def test_parse_skill(self, dream):
        """[SKILL] directive is parsed into the skills list."""
        adds, removes, skills = dream._parse_directives(
            "[SKILL] daily-standup: Generate a daily standup report"
        )
        assert adds == []
        assert removes == []
        assert skills == [("daily-standup", "Generate a daily standup report")]

    def test_parse_skill_mixed_with_file(self, dream):
        """[SKILL] works alongside FILE directives."""
        adds, removes, skills = dream._parse_directives(
            "[FILE] MEMORY.md: User runs standup every morning\n"
            "[SKILL] daily-standup: Generate a daily standup report"
        )
        assert len(adds) == 1
        assert adds[0] == ("MEMORY", "User runs standup every morning")
        assert skills == [("daily-standup", "Generate a daily standup report")]

    def test_parse_skill_bad_name_skipped(self, dream):
        """Non-kebab-case skill names are ignored by the regex."""
        adds, removes, skills = dream._parse_directives(
            "[SKILL] Bad Name: description"
        )
        assert skills == []


# ---------------------------------------------------------------------------
# _remove_match
# ---------------------------------------------------------------------------


class TestRemoveMatch:
    def test_exact_match(self, dream):
        text = "## Section\n\n- Fact A\n- Fact B\n"
        result = dream._remove_match(text, "- Fact A\n")
        assert result is not None
        assert "Fact A" not in result
        assert "Fact B" in result

    def test_no_match(self, dream):
        text = "## Section\n\n- Fact A\n"
        result = dream._remove_match(text, "- Nonexistent")
        assert result is None

    def test_multiline_block_match(self, dream):
        text = "## Stack\n\n- Python\n- Rust\n- Go\n"
        to_remove = "- Python\n- Rust\n"
        result = dream._remove_match(text, to_remove)
        assert result is not None
        assert "Python" not in result
        assert "Rust" not in result
        assert "Go" in result

    def test_empty_to_remove(self, dream):
        result = dream._remove_match("content", "")
        assert result is None


# ---------------------------------------------------------------------------
# _format_entries
# ---------------------------------------------------------------------------


class TestFormatEntries:
    def test_format_empty(self, dream):
        assert dream._format_entries([]) == ""

    def test_format_single(self, dream):
        entries = [{"cursor": 1, "timestamp": "2026-06-21 10:00", "content": "- Fact A"}]
        result = dream._format_entries(entries)
        assert "Fact A" in result
        assert "2026-06-21 10:00" in result

    def test_truncates_long_content(self, dream):
        entries = [{"cursor": 1, "timestamp": "2026", "content": "x" * 30_000}]
        result = dream._format_entries(entries)
        assert len(result) < 30_000
        assert "(truncated)" in result


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


class TestFileHelpers:
    def test_read_write_soul(self, dream, store):
        store.write_soul("I am a bot")
        assert dream._read_file("SOUL") == "I am a bot"
        dream._write_file("SOUL", "Updated soul")
        assert store.read_soul() == "Updated soul"

    def test_read_write_user(self, dream, store):
        store.write_user("User profile")
        assert dream._read_file("USER") == "User profile"
        dream._write_file("USER", "Updated user")
        assert store.read_user() == "Updated user"

    def test_read_write_memory(self, dream, store):
        store.write_memory_file("Long-term memory")
        assert dream._read_file("MEMORY") == "Long-term memory"
        dream._write_file("MEMORY", "Updated memory")
        assert store.read_memory_file() == "Updated memory"
