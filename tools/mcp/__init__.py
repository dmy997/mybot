"""MCP (Model Context Protocol) integration.

Provides MCPTool, McpConnection, and MCPClientManager for connecting
to external MCP servers and exposing their tools to the agent.
"""

from .client_manager import (
    MCPClientManager,
    MCPClientManagerEvent,
    MCPServerConfig,
    load_mcp_config,
)
from .connection import McpConnection
from .mcp_tool import MCPTool

__all__ = [
    "MCPClientManager",
    "MCPClientManagerEvent",
    "MCPServerConfig",
    "McpConnection",
    "MCPTool",
    "load_mcp_config",
]
