"""WebFetch tool — async HTTP GET with HTML-to-text conversion."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from .guard import Capability
from .tool import Tool, ToolResult

MAX_CONTENT_LENGTH = 500_000
MAX_RESPONSE_SIZE = 5_000_000
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTML → text extractor
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Extract visible text from HTML, stripping scripts/styles."""

    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "noscript", "iframe"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "iframe"):
            self._skip = max(0, self._skip - 1)
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.text.append(data)


def _extract_text(html: str) -> str:
    """Convert HTML to plain text."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class WebFetchTool(Tool):
    """Fetch a URL and return its content as plain text.

    HTML pages are converted to readable text.  JSON and plain-text
    responses are returned as-is.  Non-http/https URLs are rejected.
    """

    name = "webfetch"
    _scopes = {"core", "subagent"}
    _parallel = True
    capabilities = {Capability.NETWORK}
    description = (
        "Fetch content from a URL and return as plain text. "
        "Use for: reading documentation, API references, checking a specific "
        "web page's content. "
        "NOT for: broad information discovery (use websearch), local file "
        "reading (use read), or API calls that require authentication. "
        "HTML pages are converted to readable text (scripts/styles removed). "
        "JSON and plain-text responses are returned as-is. "
        "Non-http/https URLs are rejected."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch (must use http or https scheme).",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def execute(self, url: str, **_: Any) -> ToolResult:
        url = url.strip()
        if not url:
            return ToolResult(success=False, content="", error="URL must not be empty")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(
                success=False,
                content="",
                error=f"Only http/https URLs are supported, got: {parsed.scheme}",
            )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, follow_redirects=True)
                content = response.text[:MAX_RESPONSE_SIZE]
        except httpx.TimeoutException:
            return ToolResult(
                success=False, content="", error=f"Request timed out after {self._timeout}s"
            )
        except httpx.HTTPStatusError as e:
            return ToolResult(
                success=False, content="", error=f"HTTP {e.response.status_code}"
            )
        except Exception as exc:
            return ToolResult(success=False, content="", error=f"Fetch failed: {exc}")

        content_type = response.headers.get("content-type", "")

        if "text/html" in content_type:
            text = _extract_text(content)
        elif "application/json" in content_type:
            text = content
        else:
            text = content

        if len(text) > MAX_CONTENT_LENGTH:
            text = text[:MAX_CONTENT_LENGTH] + "\n... (content truncated)"

        return ToolResult(success=True, content=text)
