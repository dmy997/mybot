"""WebSearch tool — multi-source web search with provider auto-detection.

Supports DuckDuckGo (free, always available), Google Custom Search
(needs GOOGLE_API_KEY + GOOGLE_CSE_ID), and Bing (needs BING_API_KEY).
Providers are detected at init time from environment variables.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx
from loguru import logger

from .guard import Capability
from .tool import Tool, ToolResult

MAX_RESULTS = 10
DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


# ---------------------------------------------------------------------------
# HTML parser for DuckDuckGo
# ---------------------------------------------------------------------------


class _DDGResultParser(HTMLParser):
    """Extract search result titles, URLs, and snippets from DDG HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self._current: dict[str, str] = {}
        self._in_result = False
        self._in_link = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")

        if tag == "div" and "result" in cls.split():
            self._in_result = True
            self._current = {}

        if self._in_result:
            if tag == "a" and "result__a" in cls.split():
                self._in_link = True
                href = attrs_d.get("href", "")
                self._current["url"] = self._clean_url(href)
            elif tag == "a" and "result__snippet" in cls.split():
                self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_link = False
            self._in_snippet = False
        if tag == "div" and self._in_result:
            self._in_result = False
            if self._current.get("title") and self._current.get("url"):
                self.results.append(SearchResult(
                    title=self._current.get("title", ""),
                    url=self._current.get("url", ""),
                    snippet=self._current.get("snippet", ""),
                ))

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._current["title"] = (self._current.get("title", "") + data).strip()
        elif self._in_snippet:
            self._current["snippet"] = (self._current.get("snippet", "") + data).strip()

    @staticmethod
    def _clean_url(url: str) -> str:
        """Extract actual URL from DDG redirect wrapper."""
        # DDG wraps URLs as //duckduckgo.com/l/?uddg=ENCODED_URL&rut=...
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1))
        return url


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------


class SearchProvider(ABC):
    """Abstract search backend."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        ...


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo HTML search (free, no API key required)."""

    name = "duckduckgo"
    _BASE = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._BASE,
                    data={"q": query, "b": ""},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    follow_redirects=True,
                )
                html = resp.text
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: {}", exc)
            return []

        parser = _DDGResultParser()
        try:
            parser.feed(html)
        except Exception:
            pass
        return parser.results[:max_results]


class GoogleSearchProvider(SearchProvider):
    """Google Custom Search JSON API.

    Requires GOOGLE_API_KEY and GOOGLE_CSE_ID environment variables.
    """

    name = "google"
    _BASE = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cse_id: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._api_key = api_key
        self._cse_id = cse_id
        self._timeout = timeout

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        params = {
            "key": self._api_key,
            "cx": self._cse_id,
            "q": query,
            "num": min(max_results, 10),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Google search failed: {}", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("items", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            ))
        return results[:max_results]


class BingSearchProvider(SearchProvider):
    """Bing Web Search API.

    Requires BING_API_KEY environment variable.
    """

    name = "bing"
    _BASE = "https://api.bing.microsoft.com/v7.0/search"

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        headers = {"Ocp-Apim-Subscription-Key": self._api_key}
        params = {"q": query, "count": min(max_results, 50)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._BASE, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Bing search failed: {}", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("webPages", {}).get("value", []):
            results.append(SearchResult(
                title=item.get("name", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
            ))
        return results[:max_results]


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _detect_providers(timeout: int = DEFAULT_TIMEOUT) -> dict[str, SearchProvider]:
    """Build a dict of available search providers from environment config.

    DuckDuckGo is always available (free, no key).  Google and Bing are
    enabled only when their respective API keys are set.
    """
    providers: dict[str, SearchProvider] = {}

    # DuckDuckGo — always available
    providers["duckduckgo"] = DuckDuckGoProvider(timeout=timeout)

    # Google Custom Search
    google_key = os.getenv("GOOGLE_API_KEY", "")
    google_cse = os.getenv("GOOGLE_CSE_ID", "")
    if google_key and google_cse:
        providers["google"] = GoogleSearchProvider(google_key, google_cse, timeout=timeout)

    # Bing
    bing_key = os.getenv("BING_API_KEY", "")
    if bing_key:
        providers["bing"] = BingSearchProvider(bing_key, timeout=timeout)

    return providers


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class WebSearchTool(Tool):
    """Multi-source web search with automatic provider detection.

    DuckDuckGo is always available (free).  Google and Bing are enabled
    when their API keys are configured via environment variables.
    """

    name = "websearch"
    _scopes = {"core", "subagent"}
    _parallel = True
    capabilities = {Capability.NETWORK}
    description = (
        "Search the web and return results with titles, URLs, and snippets. "
        "Use this to find information, documentation, or answers to questions "
        "that require up-to-date web knowledge."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query string.",
            },
            "source": {
                "type": "string",
                "description": (
                    "Search provider to use.  Use 'auto' (default) for the first "
                    "available provider, or specify a named source.  "
                    "Available sources are listed in the tool description."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max number of results to return (default: 10, max: 10).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._providers = _detect_providers(timeout=timeout)
        # Update description to reflect available sources
        sources = list(self._providers.keys())
        self.description = (
            "Search the web and return results with titles, URLs, and snippets. "
            f"Available sources: {', '.join(sources)}. "
            "Use 'auto' to pick the first available source."
        )

    async def execute(
        self,
        query: str,
        source: str = "auto",
        max_results: int = MAX_RESULTS,
        **_: Any,
    ) -> ToolResult:
        query = query.strip()
        if not query:
            return ToolResult(success=False, content="", error="query must not be empty")

        if not self._providers:
            return ToolResult(
                success=False, content="", error="No search providers available"
            )

        # Resolve provider
        if source == "auto":
            provider = next(iter(self._providers.values()))
        elif source in self._providers:
            provider = self._providers[source]
        else:
            available = ", ".join(self._providers.keys())
            return ToolResult(
                success=False,
                content="",
                error=f"Unknown source '{source}'. Available: {available}",
            )

        max_results = max(1, min(max_results, MAX_RESULTS))

        results = await provider.search(query, max_results=max_results)

        if not results:
            return ToolResult(
                success=True,
                content=f"No results found for '{query}' (via {provider.name}).",
            )

        lines = [f"--- {len(results)} result(s) for '{query}' via {provider.name} ---"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   {r.url}")
            if r.snippet:
                lines.append(f"   {r.snippet}")
            lines.append("")

        return ToolResult(success=True, content="\n".join(lines))
