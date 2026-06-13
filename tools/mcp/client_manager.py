"""MCPClientManager — manages multiple MCP server connections and tool registration."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from tools.registry import ToolRegistry

from .connection import McpConnection, SSEServerConfig, StdioServerConfig
from .mcp_tool import MCPTool


def _kw_only_defaults() -> dict:
    return {}


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server entry."""
    name: str
    transport: str = "stdio"  # "stdio" or "sse"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=_kw_only_defaults)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=_kw_only_defaults)

    def to_connection(self) -> McpConnection:
        """Build an :class:`McpConnection` from this config."""
        if self.transport == "stdio":
            return McpConnection(
                self.name,
                stdio=StdioServerConfig(
                    command=self.command,
                    args=self.args,
                    env=self.env or None,
                    cwd=self.cwd or None,
                ),
            )
        elif self.transport == "sse":
            return McpConnection(
                self.name,
                sse=SSEServerConfig(
                    url=self.url,
                    headers=self.headers or {},
                ),
            )
        raise ValueError(f"Unknown transport {self.transport!r}")


@dataclass
class MCPClientManagerEvent:
    """Event emitted by MCPClientManager on connection state changes."""
    event: str  # "connected", "disconnected", "tools_changed"
    server_name: str
    tool_names: list[str] = field(default_factory=list)
    error: str | None = None


class MCPClientManager:
    """Manages multiple MCP server connections.

    Connects to MCP servers defined in configuration, discovers their tools,
    and registers them with a :class:`ToolRegistry`.  Handles automatic
    reconnection with configurable retry.

    Usage::

        manager = MCPClientManager(tool_registry)
        manager.configure([
            MCPServerConfig(
                name="filesystem", command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            ),
        ])
        await manager.start()
        # ... tools are now registered ...
        await manager.stop()
    """

    _RECONNECT_DELAY = 5.0
    _MAX_RECONNECT_DELAY = 60.0

    def __init__(
        self,
        tool_registry: ToolRegistry,
        *,
        on_event: Callable[[MCPClientManagerEvent], None] | None = None,
    ) -> None:
        self._registry = tool_registry
        self._on_event = on_event
        self._connections: dict[str, McpConnection] = {}
        self._server_configs: dict[str, MCPServerConfig] = {}
        self._registered_tools: dict[str, list[str]] = {}  # server_name → tool names
        self._running = False
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # -- configuration -------------------------------------------------------

    @property
    def servers(self) -> list[str]:
        """Names of configured MCP servers."""
        return list(self._server_configs.keys())

    def configure(self, servers: list[MCPServerConfig]) -> None:
        """Set the MCP server configurations.

        Call before :meth:`start`.  Replaces any previous configuration.
        """
        self._server_configs = {s.name: s for s in servers}

    def add_server(self, config: MCPServerConfig) -> None:
        """Add a single MCP server configuration."""
        self._server_configs[config.name] = config

    def remove_server(self, name: str) -> None:
        """Remove a configured MCP server (disconnects if connected)."""
        self._server_configs.pop(name, None)
        if name in self._connections:
            asyncio.create_task(self._disconnect_server(name))

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect to all configured MCP servers and register their tools."""
        if self._running:
            return
        self._running = True

        for name in self._server_configs:
            self._tasks[name] = asyncio.create_task(self._run_server_loop(name))

        logger.info("MCP client manager started ({!s} servers)", len(self._server_configs))

    async def stop(self) -> None:
        """Disconnect all MCP servers and unregister their tools."""
        self._running = False

        # Cancel background tasks — their finally blocks will clean up
        # connections inside the correct task context.
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        logger.info("MCP client manager stopped")

    # -- internal: per-server loop -------------------------------------------

    async def _run_server_loop(self, server_name: str) -> None:
        """Connect, register tools, and reconnect on failure for one server.

        All connection cleanup happens inside this task so that the
        ``stdio_client`` async context manager is always closed from
        the same task it was entered from (avoiding anyio cancel-scope
        errors).
        """
        delay = 0.0

        try:
            while self._running:
                if delay > 0:
                    await asyncio.sleep(delay)

                config = self._server_configs.get(server_name)
                if config is None:
                    break  # server was removed from config

                try:
                    conn = config.to_connection()
                    await conn.connect()
                    self._connections[server_name] = conn

                    # Discover tools and register them
                    tool_defs = await conn.list_tools()
                    self._register_server_tools(server_name, conn, tool_defs)

                    self._emit(MCPClientManagerEvent(
                        event="connected",
                        server_name=server_name,
                        tool_names=[t["name"] for t in tool_defs],
                    ))

                    delay = 0.0  # reset on successful connection

                    # Idle — wait for disconnect or stop
                    while self._running and conn.connected:
                        await asyncio.sleep(1.0)

                except Exception as exc:
                    logger.warning(
                        "MCP server {!r} error: {}. Reconnecting in {!s}s...",
                        server_name, exc, delay,
                    )
                    self._emit(MCPClientManagerEvent(
                        event="disconnected",
                        server_name=server_name,
                        error=str(exc),
                    ))

                finally:
                    # Always clean up the connection from within this task.
                    # stop() cancels us → CancelledError surfaces here, and
                    # the finally block still runs in the same task that
                    # entered the stdio_client context manager.
                    await self._cleanup_server(server_name)

                # Exponential backoff
                delay = min(
                    self._RECONNECT_DELAY if delay == 0 else delay * 2,
                    self._MAX_RECONNECT_DELAY,
                )
        except asyncio.CancelledError:
            # Final cleanup on task cancellation
            await self._cleanup_server(server_name)

    async def _cleanup_server(self, server_name: str) -> None:
        """Unregister tools and disconnect (safe to call from server task only)."""
        await self._unregister_server_tools(server_name)
        conn = self._connections.pop(server_name, None)
        if conn:
            try:
                await conn.disconnect()
            except Exception:
                logger.debug("MCP server {!r} disconnect error (ignored)", server_name)

    async def _disconnect_server(self, server_name: str) -> None:
        """Disconnect a single server and unregister its tools.

        Prefer using the in-task cleanup path.  This is a fallback for
        external callers that runs within the server loop task.
        """
        await self._cleanup_server(server_name)

    # -- tool registry management --------------------------------------------

    def _register_server_tools(
        self,
        server_name: str,
        connection: McpConnection,
        tool_defs: list[dict[str, Any]],
    ) -> None:
        """Create MCPTool wrappers and register them."""
        names: list[str] = []
        for td in tool_defs:
            tool = MCPTool(td, server_name, connection)
            self._registry.register(tool)
            names.append(tool.name)
            logger.debug(
                "Registered MCP tool {!r} from server {!r}",
                tool.name, server_name,
            )
        self._registered_tools[server_name] = names

    async def _unregister_server_tools(self, server_name: str) -> None:
        """Unregister all tools previously registered by *server_name*."""
        names = self._registered_tools.pop(server_name, [])
        for name in names:
            self._registry.unregister(name)

    # -- events ---------------------------------------------------------------

    def _emit(self, event: MCPClientManagerEvent) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                logger.exception("Error in MCP event callback")

    def __repr__(self) -> str:
        return (f"MCPClientManager(servers={list(self._server_configs)}, "
                f"connected={list(self._connections)})")


# ---------------------------------------------------------------------------
# Config loading helpers
# ---------------------------------------------------------------------------


def load_mcp_config(path: str | Path) -> list[MCPServerConfig]:
    """Load MCP server configurations from a JSON file.

    Example format::

        {
            "servers": [
                {
                    "name": "filesystem",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"HOME": "/home/user"}
                },
                {
                    "name": "remote",
                    "transport": "sse",
                    "url": "http://localhost:8000/sse"
                }
            ]
        }
    """
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    servers = data.get("servers", data.get("mcp_servers", []))

    configs: list[MCPServerConfig] = []
    for entry in servers:
        configs.append(MCPServerConfig(
            name=entry["name"],
            transport=entry.get("transport", "stdio"),
            command=entry.get("command", ""),
            args=entry.get("args", []),
            env=entry.get("env"),
            cwd=entry.get("cwd", ""),
            url=entry.get("url", ""),
            headers=entry.get("headers"),
        ))
    return configs
