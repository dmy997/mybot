"""MemoryService — owns MemoryStore + hybrid store, handles long-term memory.

Extracted from ContextManager so memory CRUD and context building can be
tested and reasoned about independently of session persistence and compression.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from memory.store import MemoryStore


class MemoryService:
    """Owns :class:`MemoryStore` and optional hybrid search store.

    Handles remember / forget / recall / memory-context assembly.
    """

    def __init__(
        self,
        workspace: Path,
        hybrid_store: object | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = MemoryStore(self.workspace, hybrid_store=hybrid_store)

    # -- memory context -------------------------------------------------------

    def build_memory_context(self) -> str:
        """Build the memory section for system-prompt injection."""
        parts: list[str] = []

        soul = self.store.read_soul()
        if soul.strip():
            parts.append(f"# Identity (SOUL.md)\n\n{soul}")

        user = self.store.read_user()
        if user.strip() and not self._is_user_template(user):
            parts.append(f"# User Profile (USER.md)\n\n{user}")

        memory_ctx = self.store.get_memory_context()
        if memory_ctx.strip():
            parts.append(memory_ctx)

        return "\n\n---\n\n".join(parts) if parts else ""

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
    ) -> None:
        """Append a fact to MEMORY.md (dedup by content)."""
        current = self.store.read_memory_file()
        if content.strip().lower() in current.lower():
            return
        entry = f"- [{mem_type}] {name}: {content}"
        if description:
            entry += f"  # {description}"
        updated = current.rstrip() + "\n" + entry + "\n"
        self.store.write_memory_file(updated)

    def forget(self, name: str) -> bool:
        """Remove a fact from MEMORY.md by name match."""
        current = self.store.read_memory_file()
        lines = current.splitlines()
        new_lines: list[str] = []
        removed = False
        for line in lines:
            if f"[{name}]" in line or (line.startswith("- ") and name.lower() in line.lower()):
                removed = True
                continue
            new_lines.append(line)
        if removed:
            self.store.write_memory_file("\n".join(new_lines) + "\n")
            return True
        return False

    def recall(
        self, query: str, *, top_n: int = 10, session_key: str | None = None,
    ) -> list[dict]:
        """Search memory content. Uses hybrid search when available."""
        import time

        if not query.strip():
            return []

        t0 = time.monotonic()
        hybrid_store = self.store._hybrid_store
        if hybrid_store is not None:
            try:
                sr = hybrid_store.search(query, top_k=top_n)
                ms = (time.monotonic() - t0) * 1000
                mode = "hybrid" if hybrid_store._has_vec else "fts5_only"
                from observability.log import MemorySearchEvent, emit
                emit(MemorySearchEvent(
                    query=query[:80], mode=mode,
                    result_count=len(sr), latency_ms=round(ms, 2),
                    session_key=session_key,
                ))
                if sr:
                    return [
                        {
                            "name": r.source_key or r.source,
                            "content": r.content,
                            "mem_type": r.source,
                            "score": r.score,
                        }
                        for r in sr
                    ]
            except Exception:
                logger.debug("Hybrid search failed, falling back to substring", exc_info=True)

        results = self._substring_recall(query, top_n=top_n)
        ms = (time.monotonic() - t0) * 1000
        from observability.log import MemorySearchEvent, emit
        emit(MemorySearchEvent(
            query=query[:80], mode="substring",
            result_count=len(results), latency_ms=round(ms, 2),
            session_key=session_key,
        ))
        return results

    def _substring_recall(self, query: str, *, top_n: int = 10) -> list[dict]:
        """Fallback: case-insensitive substring search in MEMORY.md."""
        current = self.store.read_memory_file()
        query_lower = query.lower()
        results: list[dict] = []
        for line in current.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            if query_lower not in line.lower():
                continue
            rest = line[2:]
            entry: dict = {"raw": rest}
            if rest.startswith("[") and "] " in rest:
                bracket_end = rest.index("] ")
                entry["mem_type"] = rest[1:bracket_end]
                rest = rest[bracket_end + 2:]
            if ": " in rest:
                name_part, content_part = rest.split(": ", 1)
                entry["name"] = name_part.strip()
                entry["content"] = content_part.split("  #")[0].strip()
            else:
                entry["name"] = rest.strip()
                entry["content"] = rest.strip()
            if "content" in entry:
                results.append(entry)
            if len(results) >= top_n:
                break
        return results

    @staticmethod
    def _is_user_template(content: str) -> bool:
        """Return True if USER.md content looks like an unfilled template."""
        markers = [
            "Edit this file to customize",
            "(your timezone",
            "(your role",
            "(preferred language",
        ]
        lower = content.lower()
        return any(m.lower() in lower for m in markers)
