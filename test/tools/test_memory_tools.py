"""Tests for memory tools."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from tools.memory_tools import MemoryForgetTool, MemoryRecallTool, MemoryRememberTool


class TestMemoryRememberTool:
    def test_no_ctx_returns_error(self):
        tool = MemoryRememberTool()
        result = asyncio.run(tool.execute("test", "content"))
        assert result.success is False
        assert "not available" in result.error.lower()

    def test_saves_memory(self):
        ctx = MagicMock()
        ctx.remember = MagicMock()
        tool = MemoryRememberTool(ctx)
        result = asyncio.run(tool.execute("my-key", "some content", mem_type="project"))
        assert result.success is True
        assert "saved" in result.content.lower()
        ctx.remember.assert_called_once_with(
            "my-key", "some content", mem_type="project", description=""
        )

    def test_saves_with_description(self):
        ctx = MagicMock()
        ctx.remember = MagicMock()
        tool = MemoryRememberTool(ctx)
        result = asyncio.run(
            tool.execute("key", "val", mem_type="user", description="a summary")
        )
        assert result.success is True
        ctx.remember.assert_called_once_with(
            "key", "val", mem_type="user", description="a summary"
        )

    def test_default_mem_type(self):
        ctx = MagicMock()
        ctx.remember = MagicMock()
        tool = MemoryRememberTool(ctx)
        result = asyncio.run(tool.execute("key", "val"))
        assert result.success is True
        call_kwargs = ctx.remember.call_args
        assert call_kwargs[1]["mem_type"] == "user"

    def test_handles_exception(self):
        ctx = MagicMock()
        ctx.remember = MagicMock(side_effect=RuntimeError("boom"))
        tool = MemoryRememberTool(ctx)
        result = asyncio.run(tool.execute("key", "val"))
        assert result.success is False
        assert "boom" in result.error


class TestMemoryRecallTool:
    def test_no_ctx_returns_error(self):
        tool = MemoryRecallTool()
        result = asyncio.run(tool.execute("query"))
        assert result.success is False
        assert "not available" in result.error.lower()

    def test_no_results(self):
        ctx = MagicMock()
        ctx.recall = MagicMock(return_value=[])
        tool = MemoryRecallTool(ctx)
        result = asyncio.run(tool.execute("nothing"))
        assert result.success is True
        assert "No matching" in result.content

    def test_returns_results(self):
        ctx = MagicMock()
        ctx.recall = MagicMock(return_value=[
            MagicMock(name="mem1", content="content 1", mem_type="user"),
            MagicMock(name="mem2", content="content 2", mem_type="project"),
        ])
        tool = MemoryRecallTool(ctx)
        result = asyncio.run(tool.execute("test"))
        assert result.success is True
        assert "mem1" in result.content
        assert "content 1" in result.content

    def test_handles_dict_results(self):
        ctx = MagicMock()
        ctx.recall = MagicMock(return_value=[
            {"name": "d1", "content": "c1", "mem_type": "user"},
        ])
        tool = MemoryRecallTool(ctx)
        result = asyncio.run(tool.execute("test"))
        assert result.success is True
        assert "d1" in result.content

    def test_handles_string_results(self):
        ctx = MagicMock()
        ctx.recall = MagicMock(return_value=["just a string"])
        tool = MemoryRecallTool(ctx)
        result = asyncio.run(tool.execute("test"))
        assert result.success is True


class TestMemoryForgetTool:
    def test_no_ctx_returns_error(self):
        tool = MemoryForgetTool()
        result = asyncio.run(tool.execute("name"))
        assert result.success is False
        assert "not available" in result.error.lower()

    def test_deletes_memory(self):
        ctx = MagicMock()
        ctx.forget = MagicMock(return_value=True)
        tool = MemoryForgetTool(ctx)
        result = asyncio.run(tool.execute("old-key"))
        assert result.success is True
        assert "deleted" in result.content.lower()
        ctx.forget.assert_called_once_with("old-key")

    def test_not_found(self):
        ctx = MagicMock()
        ctx.forget = MagicMock(return_value=False)
        tool = MemoryForgetTool(ctx)
        result = asyncio.run(tool.execute("missing"))
        assert result.success is True
        assert "not found" in result.content.lower()

    def test_handles_exception(self):
        ctx = MagicMock()
        ctx.forget = MagicMock(side_effect=RuntimeError("boom"))
        tool = MemoryForgetTool(ctx)
        result = asyncio.run(tool.execute("key"))
        assert result.success is False
        assert "boom" in result.error
