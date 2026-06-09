"""Tests for GrepTool."""

from __future__ import annotations

import pytest

from tools.grep_tool import GrepTool


@pytest.fixture
def grep_tool(tmp_path):
    """Create a GrepTool with a small workspace of test files."""
    # Create files
    (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "utils.py").write_text("import os\n\ndef helper():\n    pass\n")
    (tmp_path / "notes.md").write_text("# Title\n\nSome content here.\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("x = hello()\n")
    return GrepTool(workspace=tmp_path)


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_empty_pattern(self, grep_tool):
        result = await grep_tool.execute("   ")
        assert result.success is False
        assert "empty" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalid_regex(self, grep_tool):
        result = await grep_tool.execute("[invalid")
        assert result.success is False
        assert "regex" in result.error.lower()

    @pytest.mark.asyncio
    async def test_simple_match(self, grep_tool):
        result = await grep_tool.execute("hello")
        assert result.success is True
        assert "hello" in result.content
        # Should find in main.py and sub/nested.py
        assert "main.py" in result.content

    @pytest.mark.asyncio
    async def test_case_insensitive(self, grep_tool):
        (tmp_path := grep_tool.workspace)
        (tmp_path / "case_test.py").write_text("Hello World\nHELLO WORLD\nhello world\n")
        result = await grep_tool.execute("hello", ignore_case=True)
        assert result.success is True
        assert result.content.count("\n") >= 2  # at least 3 lines (hello, HELLO, Hello)

    @pytest.mark.asyncio
    async def test_case_sensitive_default(self, grep_tool):
        (tmp_path := grep_tool.workspace)
        (tmp_path / "case_test2.py").write_text("hello\nHELLO\n")
        result = await grep_tool.execute("HELLO")
        assert result.success is True
        assert "HELLO" in result.content

    @pytest.mark.asyncio
    async def test_no_match(self, grep_tool):
        result = await grep_tool.execute("nonexistent_pattern_xyz")
        assert result.success is True
        assert "No matches" in result.content

    @pytest.mark.asyncio
    async def test_glob_filter(self, grep_tool):
        result = await grep_tool.execute("def", glob="*.py")
        assert result.success is True
        # Should only find in .py files
        assert "notes.md" not in result.content

    @pytest.mark.asyncio
    async def test_glob_filter_md(self, grep_tool):
        result = await grep_tool.execute("Title", glob="*.md")
        assert result.success is True
        assert "notes.md" in result.content
        assert "main.py" not in result.content

    @pytest.mark.asyncio
    async def test_path_filter_directory(self, grep_tool):
        result = await grep_tool.execute("hello", path="sub")
        assert result.success is True
        assert "sub/nested.py" in result.content
        assert "main.py" not in result.content

    @pytest.mark.asyncio
    async def test_path_filter_file(self, grep_tool):
        result = await grep_tool.execute("def", path="main.py")
        assert result.success is True
        assert "main.py" in result.content
        assert "utils.py" not in result.content

    @pytest.mark.asyncio
    async def test_path_outside_workspace(self, grep_tool):
        result = await grep_tool.execute("x", path="../etc")
        assert result.success is False
        assert "outside workspace" in result.error.lower()

    @pytest.mark.asyncio
    async def test_path_not_found(self, grep_tool):
        result = await grep_tool.execute("x", path="nonexistent")
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_skips_hidden_files(self, grep_tool):
        (tmp_path := grep_tool.workspace)
        (tmp_path / ".hidden.py").write_text("secret\n")
        result = await grep_tool.execute("secret")
        assert result.success is True
        assert "No matches" in result.content or ".hidden" not in result.content

    @pytest.mark.asyncio
    async def test_skips_binary_extensions(self, grep_tool):
        (tmp_path := grep_tool.workspace)
        (tmp_path / "image.png").write_text("x" * 100)  # pretend binary
        result = await grep_tool.execute("x", glob="*.png")
        assert result.success is True
        assert "No matches" in result.content

    @pytest.mark.asyncio
    async def test_line_numbers(self, grep_tool):
        result = await grep_tool.execute("pass")
        assert result.success is True
        assert ":4: " in result.content  # line number in output format

    @pytest.mark.asyncio
    async def test_includes_relative_path(self, grep_tool):
        result = await grep_tool.execute("import os")
        assert result.success is True
        assert "utils.py" in result.content
