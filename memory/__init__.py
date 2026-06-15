"""Memory system — markdown file-system-based long-term memory.

Components:
- MemoryStore: Pure file I/O for MEMORY.md and individual .md files
- MemoryManager: High-level CRUD, keyword search
- MemoryEntry: Dataclass for a single memory entry
"""

from .manager import MemoryManager
from .store import MemoryStore
from .types import MEMORY_TYPES, MemoryEntry, parse_frontmatter

__all__ = [
    "MemoryStore",
    "MemoryManager",
    "MemoryEntry",
    "MEMORY_TYPES",
    "parse_frontmatter",
]
