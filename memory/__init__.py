"""Memory system — markdown file-system-based long-term memory.

Components:
- MemoryStore: Pure file I/O for SOUL.md, USER.md, MEMORY.md, history.jsonl
- MemoryProvider: ABC for pluggable memory backends
- BuiltinMemoryProvider: Wraps MemoryService as a MemoryProvider
- MemoryManager: Coordinates builtin + external providers with fault isolation
- Dream: Periodic LLM-driven memory consolidation (two-phase)
- Consolidator: Token-budget-triggered conversation summarization
- StreamingContextScrubber: Strips <memory-context> fences from streaming output
"""

from .builtin_provider import BUILTIN_TOOL_SCHEMAS, BuiltinMemoryProvider
from .manager import MemoryManager
from .provider import MemoryProvider
from .scrubber import StreamingContextScrubber, build_memory_context_block, sanitize_context
from .store import MemoryStore

__all__ = [
    "BUILTIN_TOOL_SCHEMAS",
    "BuiltinMemoryProvider",
    "MemoryManager",
    "MemoryProvider",
    "MemoryStore",
    "StreamingContextScrubber",
    "build_memory_context_block",
    "sanitize_context",
]
