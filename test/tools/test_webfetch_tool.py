"""Tests for WebFetchTool."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tools.webfetch_tool import MAX_CONTENT_LENGTH, WebFetchTool, _extract_text


class TestHTMLExtraction:
    def test_simple_html(self):
        text = _extract_text("<html><body><p>Hello World</p></body></html>")
        assert "Hello World" in text

    def test_strips_scripts(self):
        text = _extract_text("<html><script>alert(1)</script><p>content</p></html>")
        assert "alert" not in text
        assert "content" in text

    def test_strips_styles(self):
        text = _extract_text("<html><style>.x{}</style><p>text</p></html>")
        assert ".x" not in text
        assert "text" in text

    def test_br_adds_newline(self):
        text = _extract_text("<p>line1<br>line2</p>")
        assert "line1" in text
        assert "line2" in text

    def test_empty_html(self):
        text = _extract_text("")
        assert text == ""

    def test_collapses_whitespace(self):
        text = _extract_text("<p>a    b\n\n\n\nc</p>")
        # Multiple newlines collapsed
        assert "\n\n\n\n" not in text


class TestWebFetchTool:
    def test_rejects_empty_url(self):
        tool = WebFetchTool()
        result = asyncio.run(tool.execute("   "))
        assert result.success is False
        assert "empty" in result.error.lower()

    def test_rejects_non_http_scheme(self):
        tool = WebFetchTool()
        result = asyncio.run(tool.execute("ftp://example.com/file"))
        assert result.success is False
        assert "http/https" in result.error.lower()

    def test_rejects_ssh_scheme(self):
        tool = WebFetchTool()
        result = asyncio.run(tool.execute("file:///etc/passwd"))
        assert result.success is False
        assert "http/https" in result.error.lower()

    def test_validates_url(self):
        tool = WebFetchTool()
        result = asyncio.run(tool.execute("not-a-url"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        tool = WebFetchTool(timeout=1)

        async def _raise_timeout(*args, **kwargs):
            raise httpx.TimeoutException("timed out")

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=_raise_timeout))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await tool.execute("https://example.com")
            assert result.success is False
            assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_truncates_large_content(self):
        tool = WebFetchTool()

        large_text = "x" * (MAX_CONTENT_LENGTH + 1000)

        async def _mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.text = large_text
            resp.headers = {"content-type": "text/plain"}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=_mock_get))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await tool.execute("https://example.com")
            assert result.success is True
            assert len(result.content) <= MAX_CONTENT_LENGTH + 50  # some leeway for truncation message

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        tool = WebFetchTool()

        async def _mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.text = "<html><body><p>Hello</p></body></html>"
            resp.headers = {"content-type": "text/html"}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(side_effect=_mock_get))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await tool.execute("https://example.com")
            assert result.success is True
            assert "Hello" in result.content
