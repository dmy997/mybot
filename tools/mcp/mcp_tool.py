"""MCPTool — a Tool subclass that delegates to a remote MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools.tool import Tool, ToolResult

if TYPE_CHECKING:
    from .connection import McpConnection


def _strip_extra_schema_keys(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep only JSON Schema keys that OpenAI function-calling accepts.

    MCP servers may include keys like ``$schema``, ``title``, ``outputSchema``,
    or ``default`` values with complex Python objects that OpenAI rejects.
    """
    allowed = {"type", "properties", "required", "additionalProperties",
               "description", "enum", "items", "oneOf", "anyOf", "allOf",
               "minItems", "maxItems", "minLength", "maxLength",
               "minimum", "maximum", "pattern", "format"}
    return {k: v for k, v in schema.items() if k in allowed}


class MCPTool(Tool):
    """A Tool that delegates execution to a remote MCP server.

    Wraps an MCP tool definition returned by ``tools/list`` and forwards
    ``execute()`` calls to the server via :class:`McpConnection`.
    """

    _scopes: set[str] = {"core", "subagent"}
    """MCP tools are available in both core and subagent scopes."""

    _parallel: bool = True
    """MCP tools default to parallel-safe since they are stateless RPC calls."""

    def __init__(
        self,
        mcp_tool_def: dict[str, Any],
        mcp_server_name: str,
        connection: McpConnection,
    ) -> None:
        params_schema = mcp_tool_def.get("inputSchema", {})
        self.name = mcp_tool_def["name"]
        self.description = mcp_tool_def.get("description", "")
        self.parameters = _strip_extra_schema_keys(params_schema)

        self._mcp_tool_name = mcp_tool_def["name"]
        self._mcp_server_name = mcp_server_name
        self._connection = connection

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool on the remote MCP server."""
        try:
            result = await self._connection.call_tool(
                self._mcp_tool_name, kwargs,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool '{self._mcp_tool_name}' failed: {exc}",
            )

        # Extract text content from the result
        text_parts: list[str] = []
        for item in result.content:
            if hasattr(item, "text"):
                text_parts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                text_parts.append(item["text"])

        content = "\n".join(text_parts)
        is_error = getattr(result, "isError", False)

        return ToolResult(
            success=not is_error,
            content=content,
            error=content if is_error else None,
        )

    def __repr__(self) -> str:
        return (f"MCPTool(name={self.name!r}, server={self._mcp_server_name!r}, "
                f"connection={self._connection!r})")
