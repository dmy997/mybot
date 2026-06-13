"""McpConnection — manages a single MCP server connection lifecycle."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

try:
    import mcp.client.session as mcp_session
    import mcp.client.stdio as mcp_stdio
    import mcp.types as mcp_types
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


@dataclass
class StdioServerConfig:
    """Configuration for an MCP stdio-transport server."""
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None


@dataclass
class SSEServerConfig:
    """Configuration for an MCP SSE-transport server."""
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class McpConnection:
    """A managed connection to a single MCP server.

    Handles the full lifecycle: connect, initialize, list tools, call tools,
    and disconnect.  Supports both stdio (subprocess) and SSE transports.

    Usage::

        conn = McpConnection("my-server", StdioServerConfig("python", ["-m", "my_mcp"]))
        async with conn:
            tools = await conn.list_tools()
            result = await conn.call_tool("my_tool", {"arg": "value"})
    """

    def __init__(
        self,
        server_name: str,
        stdio: StdioServerConfig | None = None,
        sse: SSEServerConfig | None = None,
    ) -> None:
        if not MCP_AVAILABLE:
            raise ImportError(
                "mcp SDK is required for MCP integration. "
                "Install with: pip install mcp"
            )

        self._server_name = server_name
        self._stdio = stdio
        self._sse = sse

        # Internal state
        self._session: mcp_session.ClientSession | None = None
        self._read_stream = None
        self._write_stream = None
        self._connected = False
        self._server_info: dict[str, Any] = {}

    # -- properties ----------------------------------------------------------

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the MCP server and perform the initialize handshake."""
        if self._connected:
            return

        if self._stdio:
            await self._connect_stdio()
        elif self._sse:
            await self._connect_sse()
        else:
            raise ValueError("No transport configured (stdio or sse)")

        self._connected = True
        logger.info(
            "MCP server {!r} connected: {} v{}",
            self._server_name,
            self._server_info.get("name", "?"),
            self._server_info.get("version", "?"),
        )

    async def _connect_stdio(self) -> None:
        """Establish a stdio transport connection."""
        assert self._stdio is not None

        params = mcp_stdio.StdioServerParameters(
            command=self._stdio.command,
            args=self._stdio.args,
            env=self._stdio.env,
            cwd=self._stdio.cwd,
        )

        # Redirect MCP server stderr to /dev/null instead of the terminal
        # where it would overwrite prompt_toolkit's input area.
        mcp_stderr = open(os.devnull, "w")
        self._mcp_stderr = mcp_stderr
        stdio_ctx = mcp_stdio.stdio_client(params, errlog=mcp_stderr)
        read, write = await stdio_ctx.__aenter__()
        self._read_stream = read
        self._write_stream = write
        self._stdio_ctx = stdio_ctx

        self._session = mcp_session.ClientSession(read, write)
        await self._session.__aenter__()

        result = await self._session.initialize()
        self._server_info = {
            "name": result.serverInfo.name if result.serverInfo else "?",
            "version": str(result.serverInfo.version) if result.serverInfo else "?",
            "protocol": str(result.protocolVersion),
        }

    async def _connect_sse(self) -> None:
        """Establish an SSE transport connection."""
        assert self._sse is not None
        try:
            import mcp.client.sse as mcp_sse
        except ImportError:
            raise ImportError(
                "mcp.client.sse requires httpx-sse. Install with: pip install mcp[sse]"
            ) from None

        sse_ctx = mcp_sse.sse_client(
            url=self._sse.url,
            headers=self._sse.headers or None,
        )
        read, write = await sse_ctx.__aenter__()
        self._read_stream = read
        self._write_stream = write
        self._sse_ctx = sse_ctx

        self._session = mcp_session.ClientSession(read, write)
        await self._session.__aenter__()

        result = await self._session.initialize()
        self._server_info = {
            "name": result.serverInfo.name if result.serverInfo else "?",
            "version": str(result.serverInfo.version) if result.serverInfo else "?",
            "protocol": str(result.protocolVersion),
        }

    async def disconnect(self) -> None:
        """Close the connection to the MCP server."""
        if not self._connected:
            return

        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception:
            logger.warning("Error closing MCP session {!r}", self._server_name)

        try:
            if self._stdio and hasattr(self, "_stdio_ctx"):
                await self._stdio_ctx.__aexit__(None, None, None)
            elif self._sse and hasattr(self, "_sse_ctx"):
                await self._sse_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Error closing MCP transport {!r}", self._server_name)

        self._session = None
        self._read_stream = None
        self._write_stream = None
        self._connected = False
        if hasattr(self, "_mcp_stderr") and self._mcp_stderr is not None:
            self._mcp_stderr.close()
            self._mcp_stderr = None
        logger.info("MCP server {!r} disconnected", self._server_name)

    async def __aenter__(self) -> McpConnection:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # -- tool operations -----------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions from the connected MCP server.

        Each dict contains ``name``, ``description``, ``inputSchema`` as
        returned by the MCP ``tools/list`` request.
        """
        if not self._connected or self._session is None:
            raise RuntimeError(
                f"MCP server {self._server_name!r} is not connected"
            )

        result = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema or {},
                "title": getattr(tool, "title", None),
            }
            for tool in result.tools
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        """Call a tool on the MCP server."""
        if not self._connected or self._session is None:
            raise RuntimeError(
                f"MCP server {self._server_name!r} is not connected"
            )

        return await self._session.call_tool(name, arguments)

    def __repr__(self) -> str:
        transport = "stdio" if self._stdio else ("sse" if self._sse else "none")
        return (f"McpConnection(server={self._server_name!r}, "
                f"transport={transport}, connected={self._connected})")
