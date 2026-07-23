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
        from memory.manager import MemoryManager as _MM
        if isinstance(ctx, _MM):
            self._memory_manager = ctx
            self._ctx = None
        else:
            self._ctx = ctx
            self._memory_manager = None

    async def execute(
        self,
        name: str,
        content: str,
        mem_type: str = "user",
        description: str = "",
        **_: Any,
    ) -> ToolResult:
        if self._memory_manager is not None:
            try:
                result = await self._memory_manager.handle_tool_call(
                    "memory_remember",
                    {"name": name, "content": content, "mem_type": mem_type, "description": description},
                )
                return ToolResult(success=True, content=result["content"])
            except Exception as exc:
                return ToolResult(success=False, content="", error=f"Failed to save memory: {exc}")
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
        "Search the persistent long-term memory store by keyword. "
        "Use for: recalling user preferences, past decisions, or project "
        "context saved via memory_remember in previous sessions. "
        "NOT for: retrieving information already loaded in the system prompt "
        "(MEMORY.md, SOUL.md, USER.md are pre-loaded), recent conversation "
        "history, or code-level searches (use grep instead). "
        "Returns matching memory entries with their content."
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
        from memory.manager import MemoryManager as _MM
        if isinstance(ctx, _MM):
            self._memory_manager = ctx
            self._ctx = None
        else:
            self._ctx = ctx
            self._memory_manager = None

    async def execute(self, query: str, top_n: int = 10, **_: Any) -> ToolResult:
        if self._memory_manager is not None:
            try:
                result = await self._memory_manager.handle_tool_call(
                    "memory_recall", {"query": query, "top_n": top_n},
                )
                content = result.get("content", [])
                if not content:
                    return ToolResult(success=True, content="No matching memories found.")
                if isinstance(content, list):
                    lines = [f"--- {len(content)} result(s) for '{query}' ---"]
                    for r in content:
                        name = r.get("name", "?")
                        c = r.get("content", str(r))
                        mem_type = r.get("mem_type", "?")
                        lines.append(f"[{mem_type}] {name}: {c}")
                    return ToolResult(success=True, content="\n".join(lines))
                return ToolResult(success=True, content=str(content))
            except Exception as exc:
                return ToolResult(success=False, content="", error=f"Failed to recall: {exc}")
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
        "Use when: information is outdated, incorrect, or the user explicitly "
        "asks to forget something. "
        "NOT for: temporary hiding of information or modifying existing "
        "memories (delete then recreate with memory_remember)."
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
        from memory.manager import MemoryManager as _MM
        if isinstance(ctx, _MM):
            self._memory_manager = ctx
            self._ctx = None
        else:
            self._ctx = ctx
            self._memory_manager = None

    async def execute(self, name: str, **_: Any) -> ToolResult:
        if self._memory_manager is not None:
            try:
                result = await self._memory_manager.handle_tool_call(
                    "memory_forget", {"name": name},
                )
                return ToolResult(success=True, content=result["content"])
            except Exception as exc:
                return ToolResult(success=False, content="", error=f"Failed to forget: {exc}")
        if self._ctx is None:
            return ToolResult(success=False, content="", error="Memory system not available")
        try:
            ok = self._ctx.forget(name)
            if ok:
                return ToolResult(success=True, content=f"Memory deleted: {name}")
            return ToolResult(success=True, content=f"Memory not found: {name}")
        except Exception as exc:
            return ToolResult(success=False, content="", error=f"Failed to forget: {exc}")
