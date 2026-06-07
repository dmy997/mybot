"""Memory system — markdown file-system-based long-term memory.

Components:
- MemoryStore: Pure file I/O for MEMORY.md, individual .md files, history.jsonl
- MemoryManager: High-level CRUD, context injection, keyword search
- MemoryEntry: Dataclass for a single memory entry
"""

from .manager import MemoryManager
from .store import MemoryStore
from .types import MEMORY_TYPES, MemoryEntry, extract_links, parse_frontmatter

__all__ = [
    "MemoryStore",
    "MemoryManager",
    "MemoryEntry",
    "MEMORY_TYPES",
    "parse_frontmatter",
    "extract_links",
]
