"""MemoryService — enhanced memory management with relevance filtering.

Wraps :class:`MemoryManager` with two key additions:

1. **Relevance filtering** — when a ``provider`` is available and a ``query``
   is given, uses a lightweight LLM side-query to select the most relevant
   memories (max 5).  Falls back to keyword matching when the provider is
   unavailable.  Reference: Claude Code ``findRelevantMemories.ts``.

2. **Index truncation** — caps ``MEMORY.md`` at 200 lines / 25 KB to prevent
   unbounded system-prompt growth.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from loguru import logger

from memory.manager import MemoryManager

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_MAX_INDEX_LINES = 200
_MAX_INDEX_BYTES = 25_000
_MAX_RELEVANT_MEMORIES = 5
_MAX_MEMORY_CONTENT_CHARS = 2_000  # per-entry cap in system prompt


class MemoryService:
    """Enhanced memory manager with relevance filtering and size caps.

    Parameters
    ----------
    manager:
        The underlying :class:`MemoryManager` for CRUD operations.
    provider:
        Optional LLM provider for relevance-based memory selection.
        When ``None``, falls back to keyword matching.
    max_index_lines:
        Max lines in MEMORY.md before truncation (default 200).
    max_index_bytes:
        Max bytes in MEMORY.md before truncation (default 25 000).
    """

    def __init__(
        self,
        manager: MemoryManager,
        provider: Any | None = None,
        *,
        max_index_lines: int = _MAX_INDEX_LINES,
        max_index_bytes: int = _MAX_INDEX_BYTES,
    ) -> None:
        self.manager = manager
        self.provider = provider
        self.max_index_lines = max_index_lines
        self.max_index_bytes = max_index_bytes

    # ========================================================================
    # Public API
    # ========================================================================

    async def build_memory_context(
        self,
        *,
        query: str | None = None,
        max_memories: int = _MAX_RELEVANT_MEMORIES,
        max_content_chars: int = _MAX_MEMORY_CONTENT_CHARS,
    ) -> str:
        """Build the memory section for system-prompt injection.

        When *query* is provided and a provider is available, selects the
        most relevant memories (max *max_memories*) via LLM side-query.
        Otherwise includes all memories capped at *max_content_chars* each.

        SOUL.md and USER.md are always included.
        """
        parts: list[str] = []

        # 1. Soul file (identity) — always included
        soul = self.manager.store.read_soul()
        if soul.strip():
            parts.append(f"# Identity (SOUL.md)\n\n{soul}")

        # 2. User profile — skip template placeholders
        user = self.manager.store.read_user()
        if user.strip():
            from memory.manager import _is_template
            if not _is_template(user):
                parts.append(f"# User Profile (USER.md)\n\n{user}")

        # 3. Memory index (truncated) + relevant entries
        index = self.manager.store.read_memory_index()
        if index.strip():
            truncated_index = self._truncate_index(index)
            parts.append(f"# Memory\n\n{truncated_index}")

        # 4. Memory entry content
        all_entries = self.manager.store.list_memories()

        if all_entries:
            if query and self.provider is not None:
                # LLM-based relevance selection
                relevant = await self._select_relevant(
                    query, all_entries, max_n=max_memories,
                )
            elif query:
                # Keyword fallback (no provider)
                relevant = self.manager.recall(query, top_n=max_memories)
            else:
                # No query — include all, capped
                relevant = all_entries[:max_memories]

            for entry in relevant:
                if entry.content.strip():
                    content = entry.content.strip()
                    if len(content) > max_content_chars:
                        content = content[:max_content_chars] + "\n... (truncated)"
                    parts.append(f"### {entry.name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    # ========================================================================
    # Relevance selection
    # ========================================================================

    async def _select_relevant(
        self,
        query: str,
        entries: list[Any],
        *,
        max_n: int = _MAX_RELEVANT_MEMORIES,
    ) -> list[Any]:
        """Use a lightweight LLM call to pick memories relevant to *query*.

        Reference: Claude Code ``findRelevantMemories.ts``.
        """
        if not entries:
            return []
        if len(entries) <= max_n:
            return entries

        # Build a compact list of candidates
        candidate_lines: list[str] = []
        for i, entry in enumerate(entries):
            desc = entry.description or "(no description)"
            candidate_lines.append(f"{i}. [{entry.type}] {entry.name}: {desc}")

        candidates_text = "\n".join(candidate_lines)
        prompt = (
            "Select up to {max_n} memory entries most relevant to the user query below.\n"
            "Return ONLY a JSON array of indices (integers), e.g. [0, 3, 5].\n\n"
            "## Query\n{query}\n\n"
            "## Memory Index\n{candidates}"
        ).format(max_n=max_n, query=query[:500], candidates=candidates_text)

        try:
            response = await self.provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=50,
                temperature=0.0,
            )
            content = response.content or "[]"
            # Extract the first JSON array found
            import json as _json
            import re
            match = re.search(r"\[[\d,\s]*\]", content)
            if match:
                indices = _json.loads(match.group())
            else:
                indices = []
            return [entries[i] for i in indices if 0 <= i < len(entries)][:max_n]
        except Exception:
            logger.opt(exception=True).debug("Memory relevance selection failed, using keyword fallback")
            return self.manager.recall(query, top_n=max_n)

    # ========================================================================
    # Index truncation
    # ========================================================================

    def _truncate_index(self, content: str) -> str:
        """Truncate MEMORY.md to configured line / byte limits."""
        lines = content.splitlines()
        if len(lines) > self.max_index_lines:
            excess = len(lines) - self.max_index_lines
            lines = lines[:self.max_index_lines]
            lines.append(f"... ({excess} more entries — use /memory to view all)")

        result = "\n".join(lines)
        if len(result.encode("utf-8")) > self.max_index_bytes:
            # Truncate further to fit byte budget
            truncated = result.encode("utf-8")[:self.max_index_bytes]
            result = truncated.decode("utf-8", errors="replace")
            result += "\n... (truncated)"

        return result

    # ========================================================================
    # Delegates
    # ========================================================================

    @property
    def memory_count(self) -> int:
        return self.manager.memory_count

    def sync_from_disk(self) -> list[str]:
        return self.manager.sync_from_disk()
