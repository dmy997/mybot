"""MCPService — MCP client lifecycle management.

Extracted from Orchestrator's MCPServicesMixin so MCP connection setup,
start, and stop are testable independently of the Orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


class MCPService:
    """Owns :class:`MCPClientManager` and handles MCP server lifecycle."""

    def __init__(self, tools: object) -> None:
        self._tools = tools
        self._manager: object | None = None

    def load_config(self, config_path: Path | None, workspace: Path) -> None:
        """Load MCP config from disk and configure the client manager."""
        from tools.mcp.client_manager import MCPClientManager, load_mcp_config

        if config_path is None:
            default = workspace / "mcp_servers.json"
            if default.exists():
                config_path = default

        if config_path is not None and config_path.exists():
            try:
                servers = load_mcp_config(config_path)
                if servers:
                    self._manager = MCPClientManager(self._tools)
                    self._manager.configure(servers)
                    logger.info(
                        "MCP config loaded from {!s}: {!s} server(s)",
                        config_path, len(servers),
                    )
            except Exception:
                logger.exception("Failed to load MCP config from {!s}", config_path)

    async def start(self) -> None:
        """Connect to configured MCP servers and register their tools."""
        if self._manager is not None:
            await self._manager.start()

    async def stop(self) -> None:
        """Disconnect all MCP servers and unregister their tools."""
        if self._manager is not None:
            await self._manager.stop()
