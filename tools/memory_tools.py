"""Memory tools — LLM-callable long-term memory management.

These tools allow the agent to autonomously create, search, and delete
long-term memories.  They require a reference to the ContextManager and
are registered manually by the Orchestrator (not auto-discovered).
"""

from __future__ import annotations

from typing import Any

from .tool import Tool, ToolResult


class MemoryRememberTool(Tool):
    """Save a piece of information to long-term memory."""

    name = "memory_remember"
    _scopes = {"core"}
    _parallel = True
    capabilities = set()
    description = (
        "Save a piece of information to long-term memory. "
        "Use this to remember facts, user preferences, or important context "
        "that should persist across sessions. "
        "Memories are organized by type: 'user' (user profile/preferences), "
        "'project' (project facts/decisions), 'feedback' (user feedback on behavior), "
        "'reference' (pointers to external resources)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short kebab-case slug, e.g. 'user-language-preference'.",
            },
            "content": {
                "type": "string",
                "description": "The information to remember. Be specific and include context.",
            },
            "mem_type": {
                "type": "string",
                "enum": ["user", "project", "feedback", "reference"],
                "description": "Memory category (default: 'user').",
            },
            "description": {
                "type": "string",
                "description": "One-line summary for relevance checks (default: auto-generated).",
            },
        },
        "required": ["name", "content"],
        "additionalProperties": False,
    }

    def __init__(self, ctx: Any = None) -> None:
        self._ctx = ctx

    async def execute(
        self,
        name: str,
        content: str,
        mem_type: str = "user",
        description: str = "",
        **_: Any,
    ) -> ToolResult:
        if self._ctx is None:
            return ToolResult(success=False, content="", error="Memory system not available")
        try:
            self._ctx.remember(name, content, mem_type=mem_type, description=description)
            return ToolResult(success=True, content=f"Memory saved: {name} ({mem_type})")
        except Exception as exc:
            return ToolResult(success=False, content="", error=f"Failed to save memory: {exc}")


class MemoryRecallTool(Tool):
    """Search long-term memories by keyword."""

    name = "memory_recall"
    _scopes = {"core"}
    _parallel = True
    capabilities = set()
    description = (
        "Search long-term memories by keyword. "
        "Returns matching memory entries with their content. "
        "Use this to recall past conversations, user preferences, project decisions, "
        "or any previously saved information."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to search for in memories.",
            },
            "top_n": {
                "type": "integer",
                "description": "Max number of results to return (default: 10).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, ctx: Any = None) -> None:
        self._ctx = ctx

    async def execute(self, query: str, top_n: int = 10, **_: Any) -> ToolResult:
        if self._ctx is None:
            return ToolResult(success=False, content="", error="Memory system not available")
        try:
            results = self._ctx.recall(query, top_n=top_n)
            if not results:
                return ToolResult(success=True, content="No matching memories found.")
            lines = [f"--- {len(results)} result(s) for '{query}' ---"]
            for r in results:
                if isinstance(r, str):
                    lines.append(f"[?] {r}: {r}")
                    continue
                name = getattr(r, "name", r.get("name", "?"))
                content = getattr(r, "content", r.get("content", str(r)))
                mem_type = getattr(r, "mem_type", r.get("mem_type", "?"))
                lines.append(f"[{mem_type}] {name}: {content}")
            return ToolResult(success=True, content="\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, content="", error=f"Failed to recall: {exc}")


class MemoryForgetTool(Tool):
    """Delete a long-term memory entry."""

    name = "memory_forget"
    _scopes = {"core"}
    _parallel = True
    capabilities = set()
    description = (
        "Delete a long-term memory entry by name. "
        "Use this when information is outdated, incorrect, or the user asks to forget something."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The kebab-case slug of the memory to delete.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, ctx: Any = None) -> None:
        self._ctx = ctx

    async def execute(self, name: str, **_: Any) -> ToolResult:
        if self._ctx is None:
            return ToolResult(success=False, content="", error="Memory system not available")
        try:
            ok = self._ctx.forget(name)
            if ok:
                return ToolResult(success=True, content=f"Memory deleted: {name}")
            return ToolResult(success=True, content=f"Memory not found: {name}")
        except Exception as exc:
            return ToolResult(success=False, content="", error=f"Failed to forget: {exc}")
