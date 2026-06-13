"""Tests for tools.mcp — MCPTool, McpConnection, MCPClientManager."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp.connection import McpConnection, SSEServerConfig, StdioServerConfig
from tools.mcp.mcp_tool import MCPTool, _strip_extra_schema_keys
from tools.registry import ToolRegistry
from tools.tool import Tool

# ---------------------------------------------------------------------------
# _strip_extra_schema_keys
# --------------------------------------------------------------------------


class TestStripExtraSchemaKeys:
    def test_keeps_valid_keys(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
            "description": "test",
        }
        assert _strip_extra_schema_keys(schema) == schema

    def test_removes_unknown_keys(self):
        schema = {
            "type": "object",
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "My Tool",
            "outputSchema": {},
            "properties": {},
        }
        result = _strip_extra_schema_keys(schema)
        assert "type" in result
        assert "properties" in result
        assert "$schema" not in result
        assert "title" not in result
        assert "outputSchema" not in result

    def test_preserves_nested_allowed_keys(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                }
            },
            "oneOf": [{"type": "string"}],
            "anyOf": [{"type": "integer"}],
            "allOf": [{"type": "boolean"}],
        }
        result = _strip_extra_schema_keys(schema)
        nested = result["properties"]["items"]
        assert "items" in nested
        assert "minItems" in nested
        assert "maxItems" in nested
        assert "oneOf" in result
        assert "anyOf" in result
        assert "allOf" in result

    def test_enum_preserved(self):
        schema = {"type": "string", "enum": ["a", "b", "c"]}
        assert _strip_extra_schema_keys(schema) == schema


# ---------------------------------------------------------------------------
# MCPTool
# ---------------------------------------------------------------------------


class TestMCPTool:
    @pytest.fixture
    def mock_connection(self):
        conn = MagicMock()
        conn.server_name = "test-server"
        conn.call_tool = AsyncMock()
        return conn

    @pytest.fixture
    def sample_tool_def(self):
        return {
            "name": "echo",
            "description": "Echo back the input.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message."}
                },
                "required": ["message"],
            },
        }

    def test_construction(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)
        assert tool.name == "echo"
        assert tool.description == "Echo back the input."
        assert tool.parameters["type"] == "object"
        assert "message" in tool.parameters["properties"]
        assert tool._parallel is True
        assert "core" in tool._scopes

    def test_construct_minimal_tool_def(self, mock_connection):
        tool = MCPTool(
            {"name": "minimal", "inputSchema": {}},
            "srv", mock_connection,
        )
        assert tool.name == "minimal"
        assert tool.description == ""
        assert tool.parameters == {}

    @pytest.mark.asyncio
    async def test_execute_success(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [MagicMock()]
        mock_result.content[0].text = "hello back"
        mock_connection.call_tool.return_value = mock_result

        result = await tool.execute(message="hello")
        assert result.success is True
        assert result.content == "hello back"
        assert result.error is None
        mock_connection.call_tool.assert_called_once_with("echo", {"message": "hello"})

    @pytest.mark.asyncio
    async def test_execute_error(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)

        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = [MagicMock()]
        mock_result.content[0].text = "Tool not found"
        mock_connection.call_tool.return_value = mock_result

        result = await tool.execute()
        assert result.success is False
        assert result.error == "Tool not found"

    @pytest.mark.asyncio
    async def test_execute_connection_exception(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)
        mock_connection.call_tool.side_effect = RuntimeError("Connection lost")

        result = await tool.execute()
        assert result.success is False
        assert "Connection lost" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_dict_content(self, mock_connection, sample_tool_def):
        """MCP tools may return content as plain dicts (not Pydantic models)."""
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [{"type": "text", "text": "dict text"}]
        mock_connection.call_tool.return_value = mock_result

        result = await tool.execute()
        assert result.content == "dict text"

    @pytest.mark.asyncio
    async def test_execute_multiple_content_blocks(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)
        mock_result = MagicMock()
        mock_result.isError = False
        b1, b2 = MagicMock(), MagicMock()
        b1.text = "first"
        b2.text = "second"
        mock_result.content = [b1, b2]
        mock_connection.call_tool.return_value = mock_result

        result = await tool.execute()
        assert result.content == "first\nsecond"

    def test_repr(self, mock_connection, sample_tool_def):
        tool = MCPTool(sample_tool_def, "test-server", mock_connection)
        r = repr(tool)
        assert "MCPTool" in r
        assert "echo" in r
        assert "test-server" in r


# ---------------------------------------------------------------------------
# McpConnection config
# ---------------------------------------------------------------------------


class TestMcpConnection:
    def test_requires_mcp_installed(self):
        with patch("tools.mcp.connection.MCP_AVAILABLE", False):
            with pytest.raises(ImportError, match="mcp SDK"):
                McpConnection("test", stdio=StdioServerConfig(command="python"))

    def test_stdio_config(self):
        conn = McpConnection(
            "fs",
            stdio=StdioServerConfig(command="python", args=["-c", "print(1)"],
                                    env={"KEY": "VALUE"}, cwd="/tmp"),
        )
        assert conn.server_name == "fs"
        assert conn.connected is False

    def test_sse_config(self):
        conn = McpConnection(
            "remote",
            sse=SSEServerConfig(url="http://localhost:8000/sse",
                               headers={"Authorization": "Bearer x"}),
        )
        assert conn.server_name == "remote"
        assert conn.connected is False

    @pytest.mark.asyncio
    async def test_no_transport_raises_on_connect(self):
        conn = McpConnection("bad")
        with pytest.raises(ValueError, match="No transport"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_list_tools_not_connected_raises(self):
        conn = McpConnection("test", stdio=StdioServerConfig(command="python"))
        with pytest.raises(RuntimeError, match="not connected"):
            await conn.list_tools()

    @pytest.mark.asyncio
    async def test_call_tool_not_connected_raises(self):
        conn = McpConnection("test", stdio=StdioServerConfig(command="python"))
        with pytest.raises(RuntimeError, match="not connected"):
            await conn.call_tool("x", {})

    def test_repr(self):
        conn = McpConnection(
            "srv", stdio=StdioServerConfig(command="python")
        )
        r = repr(conn)
        assert "McpConnection" in r
        assert "srv" in r
        assert "stdio" in r

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        """Should not raise when disconnecting an idle connection."""
        conn = McpConnection("test", stdio=StdioServerConfig(command="python"))
        await conn.disconnect()


# ---------------------------------------------------------------------------
# MCPTool as proper Tool subclass
# ---------------------------------------------------------------------------


class TestMCPToolAsTool:
    def test_is_tool_subclass(self):
        assert issubclass(MCPTool, Tool)

    def test_to_openai_schema(self):
        conn = MagicMock()
        tool = MCPTool(
            {"name": "t1", "description": "Desc.", "inputSchema": {"type": "object"}},
            "srv", conn,
        )
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "t1"
        assert schema["function"]["description"] == "Desc."

    def test_available_in_core(self):
        conn = MagicMock()
        tool = MCPTool(
            {"name": "t1", "inputSchema": {}},
            "srv", conn,
        )
        assert tool.available_in("core") is True
        assert tool.available_in("subagent") is True

    def test_register_and_execute(self):
        """MCPTool works through ToolRegistry."""
        registry = ToolRegistry()
        conn = MagicMock()
        conn.server_name = "srv"

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [MagicMock(text="result")]
        conn.call_tool = AsyncMock(return_value=mock_result)

        tool = MCPTool(
            {"name": "mcp_tool", "description": "An MCP tool",
             "inputSchema": {"type": "object", "properties": {}}},
            "srv", conn,
        )
        registry.register(tool)

        assert "mcp_tool" in registry
        assert registry.get("mcp_tool") is tool

        defs = registry.get_definitions()
        assert any(d["function"]["name"] == "mcp_tool" for d in defs)


# ---------------------------------------------------------------------------
# MCPClientManager
# ---------------------------------------------------------------------------


class TestMCPClientManagerConfig:
    def test_configure(self):
        from tools.mcp.client_manager import MCPClientManager, MCPServerConfig

        registry = ToolRegistry()
        manager = MCPClientManager(registry)
        manager.configure([
            MCPServerConfig(name="s1", command="python", args=["-m", "srv"]),
            MCPServerConfig(name="s2", transport="sse", url="http://localhost/sse"),
        ])
        assert manager.servers == ["s1", "s2"]

    def test_add_remove_server(self):
        from tools.mcp.client_manager import MCPClientManager, MCPServerConfig

        registry = ToolRegistry()
        manager = MCPClientManager(registry)
        manager.add_server(MCPServerConfig(name="x", command="ls"))
        assert "x" in manager.servers
        manager.remove_server("x")
        assert "x" not in manager.servers

    def test_repr(self):
        from tools.mcp.client_manager import MCPClientManager, MCPServerConfig

        registry = ToolRegistry()
        manager = MCPClientManager(registry)
        manager.add_server(MCPServerConfig(name="x", command="ls"))
        r = repr(manager)
        assert "ls" in r or "x" in r


# ---------------------------------------------------------------------------
# load_mcp_config
# ---------------------------------------------------------------------------


class TestLoadMCPConfig:
    def test_load_from_servers_key(self):
        from tools.mcp.client_manager import load_mcp_config

        data = json.dumps({
            "servers": [
                {"name": "s1", "command": "python", "args": ["-m", "mcp"]},
                {"name": "s2", "transport": "sse", "url": "http://localhost:8000/sse"},
            ]
        })
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(data)
            f.flush()
            configs = load_mcp_config(f.name)
        Path(f.name).unlink()

        assert len(configs) == 2
        assert configs[0].name == "s1"
        assert configs[0].transport == "stdio"
        assert configs[0].command == "python"
        assert configs[0].args == ["-m", "mcp"]
        assert configs[1].name == "s2"
        assert configs[1].transport == "sse"
        assert configs[1].url == "http://localhost:8000/sse"

    def test_load_from_mcp_servers_key(self):
        from tools.mcp.client_manager import load_mcp_config

        data = json.dumps({
            "mcp_servers": [
                {"name": "only", "command": "echo"},
            ]
        })
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(data)
            f.flush()
            configs = load_mcp_config(f.name)
        Path(f.name).unlink()

        assert len(configs) == 1
        assert configs[0].name == "only"

    def test_load_empty(self):
        from tools.mcp.client_manager import load_mcp_config

        data = json.dumps({"servers": []})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(data)
            f.flush()
            configs = load_mcp_config(f.name)
        Path(f.name).unlink()

        assert configs == []

    def test_to_connection_stdio(self):
        from tools.mcp.client_manager import MCPServerConfig

        config = MCPServerConfig(
            name="fs", command="npx",
            args=["-y", "@mcp/server-filesystem", "/tmp"],
            env={"HOME": "/home/user"},
            cwd="/work",
        )
        conn = config.to_connection()
        assert conn.server_name == "fs"
        assert conn._stdio is not None
        assert conn._stdio.command == "npx"
        assert conn._stdio.args == ["-y", "@mcp/server-filesystem", "/tmp"]

    def test_to_connection_sse(self):
        from tools.mcp.client_manager import MCPServerConfig

        config = MCPServerConfig(
            name="remote", transport="sse",
            url="http://localhost:8000/sse",
            headers={"Authorization": "Bearer tok"},
        )
        conn = config.to_connection()
        assert conn.server_name == "remote"
        assert conn._sse is not None
        assert conn._sse.url == "http://localhost:8000/sse"

    def test_to_connection_unknown_transport_raises(self):
        from tools.mcp.client_manager import MCPServerConfig

        config = MCPServerConfig(name="bad", transport="grpc")
        with pytest.raises(ValueError, match="Unknown transport"):
            config.to_connection()


# ---------------------------------------------------------------------------
# MCPClientManager events + lifecycle
# ---------------------------------------------------------------------------


class TestMCPClientManagerLifecycle:
    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        """stop() is a no-op when nothing is running."""
        from tools.mcp.client_manager import MCPClientManager

        registry = ToolRegistry()
        manager = MCPClientManager(registry)
        await manager.stop()

    def test_event_callback(self):
        from tools.mcp.client_manager import MCPClientManager

        events = []

        def _on_event(event):
            events.append(event)

        registry = ToolRegistry()
        manager = MCPClientManager(registry, on_event=_on_event)
        manager._emit(
            type(manager)  # just a placeholder
        )
        # Emit with internal method
        from tools.mcp.client_manager import MCPClientManagerEvent
        manager._emit(MCPClientManagerEvent(
            event="connected", server_name="test", tool_names=["t1"],
        ))
        assert len(events) == 2
        assert events[1].event == "connected"
        assert events[1].server_name == "test"
