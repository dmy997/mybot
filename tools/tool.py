"""Tool base class and result type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .guard import Capability


@dataclass
class ToolResult:
    """Result from executing a tool."""

    success: bool
    content: str
    error: str | None = None


class Tool(ABC):
    """Abstract base for a callable tool.

    Subclasses must set ``name``, ``description``, and ``parameters``
    (JSON Schema dict), and implement ``execute(**kwargs) -> ToolResult``.

    Optional class attributes
    -------------------------
    ``_scopes``
        ``set[str]`` — contexts in which this tool is available.
        ``"core"`` (main agent), ``"subagent"`` (delegated agents),
        ``"memory"`` (dream / memory processing).
        Default: ``{"core", "subagent", "memory"}``.

    ``_parallel``
        ``bool`` — whether this tool can run concurrently with other
        invocations.  Set to ``False`` for tools that mutate shared state
        (write, bash).  Default: ``True``.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    _scopes: set[str] = {"core", "subagent", "memory"}
    """Contexts this tool is available in."""

    _parallel: bool = True
    """If True, this tool can be executed concurrently with other tool calls."""

    capabilities: set[Capability] = set()
    """What this tool can do.  Empty set = pure computation (no restrictions).
    Declare the relevant capabilities so ToolGuard can apply the correct
    security checks at execution time."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        ...

    @property
    def parallel(self) -> bool:
        """Whether this tool can be executed concurrently."""
        return self._parallel

    @property
    def scopes(self) -> set[str]:
        """Context scopes this tool is available in."""
        return self._scopes

    def available_in(self, scope: str) -> bool:
        """Return True if this tool is available in *scope*."""
        return scope in self._scopes

    def to_openai_schema(self) -> dict[str, Any]:
        """Return the OpenAI function-calling schema dict."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
