"""Tool base class and result type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


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
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        ...

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
