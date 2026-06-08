"""ToolRegistry — manages registered tools and dispatches calls."""

from __future__ import annotations

from typing import Any

from loguru import logger

from .guard import ToolGuard
from .tool import Tool, ToolResult


class ToolRegistry:
    """Registry of callable tools for the agent.

    Tools are keyed by name.  The registry produces OpenAI-compatible
    function-calling schema lists and dispatches tool calls by name.
    """

    def __init__(self, guard: ToolGuard | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.guard = guard

    # -- registration ----------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # -- scope filtering -------------------------------------------------------

    def for_scope(self, scope: str) -> list[Tool]:
        """Return tools that are available in *scope*."""
        return [t for t in self._tools.values() if t.available_in(scope)]

    def get_definitions_for_scope(self, scope: str) -> list[dict[str, Any]]:
        """Return tool definitions for tools available in *scope*."""
        return [t.to_openai_schema() for t in self.for_scope(scope)]

    # -- schema export ---------------------------------------------------------

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return the OpenAI tool definitions list for all registered tools."""
        return [t.to_openai_schema() for t in self._tools.values()]

    # -- execution -------------------------------------------------------------

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool by name with the given keyword arguments."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, content="", error=f"Unknown tool: {name}")

        # --- ToolGuard pre-check -------------------------------------------
        if self.guard is not None:
            allowed, reason = self.guard.pre_check(
                tool.name, tool.capabilities, arguments,
            )
            if not allowed:
                return ToolResult(success=False, content="", error=reason)

        # --- execute -------------------------------------------------------
        try:
            return await tool.execute(**arguments)
        except Exception as exc:
            logger.opt(exception=exc).warning("Tool '{}' execution failed", name)
            return ToolResult(
                success=False,
                content="",
                error=f"Tool '{name}' raised: {exc}",
            )

    # -- dunder ----------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def __bool__(self) -> bool:
        return bool(self._tools)
