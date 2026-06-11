"""MemoryManager — high-level API for the agent memory system.

Wraps MemoryStore with convenience methods for CRUD operations,
context injection, keyword search, and history recording.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from .store import MemoryStore
from .types import MEMORY_TYPES, MemoryEntry


_TEMPLATE_MARKERS = [
    "Edit this file to customize",
    "(your timezone",
    "(your role",
    "(preferred language",
]


def _is_template(content: str) -> bool:
    """Return True if *content* looks like an unfilled user-profile template."""
    return any(marker.lower() in content.lower() for marker in _TEMPLATE_MARKERS)


class MemoryManager:
    """High-level memory API for the agent.

    Usage::

        store = MemoryStore(workspace=Path.cwd())
        mgr = MemoryManager(store)

        # Create/update a memory
        mgr.remember("user-role", "I'm a Python backend engineer.",
                      mem_type="user", description="User's role")

        # Retrieve for context injection
        context = mgr.build_memory_context()

        # Record conversation
        mgr.record("User asked about the memory system design.")

        # Simple keyword search
        results = mgr.recall("Python engineer")
    """

    def __init__(self, store: MemoryStore):
        self.store = store

    # -- CRUD ------------------------------------------------------------------

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
        force: bool = False,
    ) -> MemoryEntry:
        """Create or update a memory entry.

        Args:
            name: Kebab-case slug for the memory.
            content: Markdown body (without frontmatter).
            mem_type: One of user, feedback, project, reference.
            description: One-line summary for the MEMORY.md index.
            force: If True, overwrite existing without warning.

        Returns:
            The created/updated MemoryEntry.
        """
        if mem_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {mem_type}. Use one of {MEMORY_TYPES}")

        existing = self.store.read_memory(name)
        if existing and not force:
            logger.info("Memory '{}' already exists, updating.", name)

        desc = description if description else (existing.description if existing else "")
        entry = MemoryEntry(
            name=name,
            type=mem_type,
            description=desc,
            content=content,
        )
        self.store.write_memory(entry)
        logger.debug("Memory '{}' saved (type={})", name, mem_type)
        return entry

    def forget(self, name: str) -> bool:
        """Delete a memory entry. Returns True if deleted."""
        deleted = self.store.delete_memory(name)
        if deleted:
            logger.debug("Memory '{}' deleted.", name)
        else:
            logger.warning("Memory '{}' not found for deletion.", name)
        return deleted

    def recall(self, query: str, *, top_n: int = 10) -> list[MemoryEntry]:
        """Simple keyword-based memory retrieval.

        Matches against name, description, and content. For V1 this is a
        basic case-insensitive substring search; upgrade to embeddings later.
        """
        query_lower = query.lower()
        scored: list[tuple[int, MemoryEntry]] = []

        for entry in self.store.list_memories():
            score = 0
            if query_lower in entry.name.lower():
                score += 10
            if query_lower in entry.description.lower():
                score += 5
            content_lower = entry.content.lower()
            score += content_lower.count(query_lower) * 2
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_n]]

    def list_all(self) -> list[MemoryEntry]:
        """List all memory entries."""
        return self.store.list_memories()

    def get(self, name: str) -> MemoryEntry | None:
        """Get a single memory by name."""
        return self.store.read_memory(name)

    # -- context injection -----------------------------------------------------

    def build_memory_context(self, *, query: str | None = None) -> str:
        """Build the memory section for injection into the agent's system prompt.

        Args:
            query: Optional current conversation topic for relevance filtering.
                   When provided, relevant memories are surfaced first.

        Returns:
            Formatted markdown string for the system prompt.
        """
        parts: list[str] = []

        # Soul file (identity)
        soul = self.store.read_soul()
        if soul.strip():
            parts.append(f"# Identity (SOUL.md)\n\n{soul}")

        # User profile — skip if it looks like an unfilled template
        user = self.store.read_user()
        if user.strip() and not _is_template(user):
            parts.append(f"# User Profile (USER.md)\n\n{user}")

        # Memory index + full content of each memory
        index = self.store.read_memory_index()
        if index.strip():
            parts.append(f"# Memory\n\n{index}")
        for entry in self.store.list_memories():
            if entry.content.strip():
                parts.append(f"### {entry.name}\n\n{entry.content}")

        return "\n\n---\n\n".join(parts)

    def find_relevant(self, query: str, *, top_n: int = 5) -> list[MemoryEntry]:
        """Find memories relevant to a query (alias for recall)."""
        return self.recall(query, top_n=top_n)

    # -- history ---------------------------------------------------------------

    def record(self, content: str, *, max_chars: int | None = None) -> int:
        """Record an entry in conversation history. Returns the cursor."""
        return self.store.append_history(content, max_chars=max_chars)

    def get_recent_history(self, count: int = 50) -> list[dict[str, Any]]:
        """Get the most recent N history entries."""
        cursor = self.store.get_last_cursor()
        since = max(0, cursor - count)
        return self.store.read_unprocessed_history(since)

    def format_recent_history(self, count: int = 50,
                              max_chars: int = 32_000) -> str:
        """Format recent history entries as a markdown string.

        Caps total output at max_chars.
        """
        entries = self.get_recent_history(count)
        lines = [f"- [{e['timestamp']}] {e['content']}" for e in entries]
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text

    # -- maintenance -----------------------------------------------------------

    def sync_from_disk(self) -> list[str]:
        """Detect externally-modified memory files and rebuild index if needed.

        Returns list of changed memory names.
        """
        changed = self.store.check_reverse_sync()
        if changed:
            logger.info(
                "Reverse-sync: {} memory files modified externally, rebuilding index.",
                len(changed),
            )
            self.store.rebuild_index()
        return changed

    def compact(self) -> None:
        """Compact history if it exceeds the max entries threshold."""
        self.store.compact_history()

    # -- statistics ------------------------------------------------------------

    @property
    def memory_count(self) -> int:
        return len(self.store.list_memories())

    @property
    def history_count(self) -> int:
        return len(self.store._read_entries())
