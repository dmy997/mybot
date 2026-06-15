"""MemoryManager — high-level API for the agent memory system.

Wraps MemoryStore with convenience methods for CRUD operations,
keyword search, and context injection.
"""

from __future__ import annotations

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

    # -- statistics ------------------------------------------------------------

    @property
    def memory_count(self) -> int:
        return len(self.store.list_memories())
