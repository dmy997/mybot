"""Memory system — markdown file-system-based long-term memory.

Components:
- MemoryStore: Pure file I/O for SOUL.md, USER.md, MEMORY.md, history.jsonl
- Dream: Periodic LLM-driven memory consolidation (two-phase)
- Consolidator: Token-budget-triggered conversation summarization
"""

from .store import MemoryStore

__all__ = ["MemoryStore"]
