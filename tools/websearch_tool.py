"""WebSearch tool — multi-source web search with provider auto-detection.

Supports DuckDuckGo (free, always available), Google Custom Search
(needs GOOGLE_API_KEY + GOOGLE_CSE_ID), Bing (needs BING_API_KEY),
and Tavily (needs TAVILY_API_KEY).
Providers are detected at init time from environment variables.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from config import Config
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote

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

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
    }

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._HEADERS, follow_redirects=True,
            ) as client:
                resp = await client.get(f"{self._BASE}?q={quote(query)}")
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


class TavilySearchProvider(SearchProvider):
    """Tavily Search API.

    Requires TAVILY_API_KEY environment variable.
    """

    name = "tavily"
    _BASE = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        body = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._BASE,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Tavily search failed: {}", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            ))
        return results[:max_results]


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _detect_providers(timeout: int = DEFAULT_TIMEOUT) -> dict[str, SearchProvider]:
    """Build a dict of available search providers from environment config.

    Registration order determines priority: API-key providers come first,
    DuckDuckGo is the last-resort fallback (free, but unreliable due to
    bot-detection challenges).
    """
    providers: dict[str, SearchProvider] = {}

    # Tavily — preferred (AI-optimized, reliable)
    tavily_key = Config.tavily_api_key
    if tavily_key:
        providers["tavily"] = TavilySearchProvider(tavily_key, timeout=timeout)

    # Google Custom Search
    google_key = Config.google_api_key
    google_cse = Config.google_cse_id
    if google_key and google_cse:
        providers["google"] = GoogleSearchProvider(google_key, google_cse, timeout=timeout)

    # Bing
    bing_key = Config.bing_api_key
    if bing_key:
        providers["bing"] = BingSearchProvider(bing_key, timeout=timeout)

    # DuckDuckGo — always available, last resort (unreliable bot detection)
    providers["duckduckgo"] = DuckDuckGoProvider(timeout=timeout)

    return providers


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class WebSearchTool(Tool):
    """Multi-source web search with automatic provider detection.

    DuckDuckGo is always available (free).  Google, Bing, and Tavily are
    enabled when their respective API keys are configured via environment
    variables.

    Supports single-source (``source="auto"`` or named provider) and
    multi-source (``source="all"``) modes.  Multi-source queries all
    available providers in parallel, deduplicates by URL, and merges
    results — useful for comprehensive research, fact-checking, or when
    a single engine may miss relevant results.
    """

    name = "websearch"
    _scopes = {"core", "subagent"}
    _parallel = True
    capabilities = {Capability.NETWORK}
    description = (
        "Search the web and return results with titles, URLs, and snippets. "
        "Use for: finding current information, documentation lookup, fact-checking, "
        "researching topics beyond training data. "
        "NOT for: fetching a specific URL's full content (use webfetch), "
        "local file search (use grep), or searching memories (use memory_recall). "
        "Supports multiple search backends (duckduckgo, google, bing, tavily)."
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
                    "Search provider to use.  Options:\n"
                    "- 'auto' (default): use the first available provider.\n"
                    "- Provider name: 'duckduckgo', 'google', 'bing', 'tavily'.\n"
                    "- 'all': query ALL available providers in parallel, "
                    "deduplicate by URL, and merge results.  "
                    "Use this for comprehensive research when a single engine "
                    "may miss relevant results, or when you need to cross-check "
                    "facts across multiple sources."
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
            "Use 'auto' to pick the first available source, "
            "or 'all' to query every available source in parallel."
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

        max_results = max(1, min(max_results, MAX_RESULTS))

        # ---- multi-source mode ----
        if source == "all":
            return await self._search_all(query, max_results)

        # ---- single-source mode ----
        if source == "auto":
            provider = next(iter(self._providers.values()))
        elif source in self._providers:
            provider = self._providers[source]
        else:
            available = ", ".join(self._providers.keys())
            return ToolResult(
                success=False,
                content="",
                error=f"Unknown source '{source}'. Available: {available}, all",
            )

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

    async def _search_all(self, query: str, max_results: int) -> ToolResult:
        """Query all available providers in parallel, deduplicate, and merge."""
        import asyncio

        tasks = {
            name: provider.search(query, max_results=max_results)
            for name, provider in self._providers.items()
        }
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)

        # Collect per-provider results, silently dropping errors
        per_provider: dict[str, list[SearchResult]] = {}
        for (name, _), raw in zip(tasks.items(), gathered):
            if isinstance(raw, BaseException):
                logger.warning("Provider {!r} failed in multi-source search: {}", name, raw)
                continue
            if raw:  # non-empty list
                per_provider[name] = raw

        if not per_provider:
            return ToolResult(
                success=True,
                content=f"No results found for '{query}' across all providers.",
            )

        # Deduplicate by URL, keeping the first occurrence (higher-priority provider)
        seen: set[str] = set()
        deduped: list[tuple[SearchResult, str]] = []  # (result, provider_name)
        for name, results in per_provider.items():
            for r in results:
                key = r.url.lower().rstrip("/")
                if key not in seen:
                    seen.add(key)
                    deduped.append((r, name))

        total_before = sum(len(v) for v in per_provider.values())
        deduped = deduped[:max_results]

        provider_list = ", ".join(per_provider.keys())
        lines = [
            f"--- Multi-source search for '{query}' ---",
            f"Providers: {provider_list}",
            f"Total: {total_before} raw, {len(deduped)} unique shown (max {max_results})",
            "",
        ]
        for i, (r, src) in enumerate(deduped, 1):
            lines.append(f"{i}. [{src}] {r.title}")
            lines.append(f"   {r.url}")
            if r.snippet:
                lines.append(f"   {r.snippet}")
            lines.append("")

        return ToolResult(success=True, content="\n".join(lines))
