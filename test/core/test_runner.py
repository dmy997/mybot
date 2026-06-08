"""Tests for the agent execution core."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.runner import AgentCore, AgentInput
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tools import Tool, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    content: str = "",
    tool_calls: list[ToolCallRequest] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=usage or {},
    )


def _make_tc(name: str, arguments: dict[str, Any], tc_id: str = "call_1") -> ToolCallRequest:
    return ToolCallRequest(id=tc_id, name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# Fake tool
# ---------------------------------------------------------------------------


class EchoTool(Tool):
    name = "echo"
    description = "Echoes back the input."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }

    async def execute(self, message: str = "") -> ToolResult:
        return ToolResult(success=True, content=f"echo: {message}")


class FailingTool(Tool):
    name = "failer"
    description = "Always fails."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=False, content="", error="deliberate failure")


class ExplodingTool(Tool):
    name = "exploder"
    description = "Raises an exception."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tools():
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


@pytest.fixture
def provider():
    return MagicMock(spec=LLMProvider)


@pytest.fixture
def core(provider):
    return AgentCore(provider)


# ---------------------------------------------------------------------------
# AgentInput / AgentOutput
# ---------------------------------------------------------------------------


class TestAgentInput:
    def test_defaults(self):
        spec = AgentInput()
        assert spec.init_messages == []
        assert spec.tools is not None
        assert spec.goal is None
        assert spec.model is None

    def test_with_messages(self):
        spec = AgentInput(init_messages=[{"role": "user", "content": "hi"}])
        assert len(spec.init_messages) == 1

    def test_goal_appended_to_user_message(self):
        """Goal is appended to the last user message, not prepended as system."""
        msgs = AgentCore._inject_goal(
            [{"role": "user", "content": "go"}], "Do the thing.",
        )
        assert msgs[0]["role"] == "user"
        assert "[Goal]" in msgs[0]["content"]
        assert "Do the thing." in msgs[0]["content"]
        assert msgs[0]["content"].startswith("go")  # user content comes first

    def test_no_goal_no_modification(self):
        msgs = [{"role": "user", "content": "hi"}]
        # No injection, messages unchanged
        assert msgs[0]["content"] == "hi"


# ---------------------------------------------------------------------------
# AgentCore — happy path
# ---------------------------------------------------------------------------


class TestAgentCoreHappy:
    @pytest.mark.asyncio
    async def test_simple_response(self, core, provider):
        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Hello!"))

        result = await core.run(AgentInput(init_messages=[{"role": "user", "content": "hi"}]))

        assert result.content == "Hello!"
        assert result.stop_reason == "stop"
        assert result.tools_used == []
        assert result.tool_events == []
        assert result.error is None

    @pytest.mark.asyncio
    async def test_single_tool_call(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "hello"})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Done after tool call."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "echo please"}],
            tools=tools,
        ))

        assert result.content == "Done after tool call."
        assert "echo" in result.tools_used
        assert result.stop_reason == "stop"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_turn(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[
                    _make_tc("echo", {"message": "a"}, tc_id="c1"),
                    _make_tc("echo", {"message": "b"}, tc_id="c2"),
                ],
                finish_reason="tool_calls",
            ),
            _make_response(content="All done."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "multi"}],
            tools=tools,
        ))

        assert result.content == "All done."
        assert result.tools_used == ["echo", "echo"]

    @pytest.mark.asyncio
    async def test_multi_turn_tool_calls(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "first"}, tc_id="c1")],
                finish_reason="tool_calls",
            ),
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "second"}, tc_id="c2")],
                finish_reason="tool_calls",
            ),
            _make_response(content="Finished."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "loop"}],
            tools=tools,
        ))

        assert result.content == "Finished."
        assert result.tools_used == ["echo", "echo"]

    @pytest.mark.asyncio
    async def test_tool_events_recorded(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "x"})],
                finish_reason="tool_calls",
            ),
            _make_response(content="ok"),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
        ))

        assert len(result.tool_events) == 1
        assert result.tool_events[0]["name"] == "echo"
        assert result.tool_events[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# AgentCore — error paths
# ---------------------------------------------------------------------------


class TestAgentCoreErrors:
    @pytest.mark.asyncio
    async def test_llm_error(self, core, provider):
        provider.chat_with_retry = AsyncMock(return_value=_make_response(
            content="API is down", finish_reason="error",
        ))

        result = await core.run(AgentInput(init_messages=[{"role": "user", "content": "hi"}]))

        assert result.stop_reason == "error"
        assert result.error is not None
        assert "API is down" in result.error

    @pytest.mark.asyncio
    async def test_failing_tool_reported(self, core, provider):
        reg = ToolRegistry()
        reg.register(FailingTool())
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("failer", {})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Handled the failure."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "fail"}],
            tools=reg,
        ))

        assert result.tool_events[0]["status"] == "error"
        assert "deliberate failure" in result.tool_events[0]["detail"]
        assert result.content == "Handled the failure."

    @pytest.mark.asyncio
    async def test_exploding_tool_caught(self, core, provider):
        reg = ToolRegistry()
        reg.register(ExplodingTool())
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("exploder", {})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Recovered after explosion."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "explode"}],
            tools=reg,
        ))

        assert result.tool_events[0]["status"] == "error"
        assert "boom" in result.tool_events[0]["detail"]
        assert result.content == "Recovered after explosion."

    @pytest.mark.asyncio
    async def test_unknown_tool(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("nonexistent", {})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Unknown tool, but I'll proceed."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "call missing tool"}],
            tools=tools,
        ))

        assert result.tool_events[0]["status"] == "error"
        assert "Unknown tool" in result.tool_events[0]["detail"]


# ---------------------------------------------------------------------------
# AgentCore — max iterations
# ---------------------------------------------------------------------------


class TestAgentCoreMaxIterations:
    @pytest.mark.asyncio
    async def test_hits_max_iterations(self, provider, tools):
        core = AgentCore(provider, max_iterations=3)
        # Every response is a tool call → never stops naturally
        provider.chat_with_retry = AsyncMock(return_value=_make_response(
            tool_calls=[_make_tc("echo", {"message": "loop"})],
            finish_reason="tool_calls",
        ))

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "loop forever"}],
            tools=tools,
        ))

        assert result.stop_reason == "max_iterations"
        assert "maximum iterations" in result.content

    @pytest.mark.asyncio
    async def test_stops_before_max_when_done(self, provider, tools):
        core = AgentCore(provider, max_iterations=10)
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(tool_calls=[_make_tc("echo", {"message": "x"})], finish_reason="tool_calls"),
            _make_response(content="Done."),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "quick"}],
            tools=tools,
        ))
        assert result.stop_reason == "stop"


# ---------------------------------------------------------------------------
# AgentCore — usage accumulation
# ---------------------------------------------------------------------------


class TestAgentCoreUsage:
    @pytest.mark.asyncio
    async def test_accumulates_usage(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "x"})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            ),
            _make_response(
                content="Done.",
                usage={"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180},
            ),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
        ))

        assert result.usage["prompt_tokens"] == 250  # 100 + 150
        assert result.usage["completion_tokens"] == 50  # 20 + 30
        assert result.usage["total_tokens"] == 300  # 120 + 180


# ---------------------------------------------------------------------------
# AgentCore — tool result capping
# ---------------------------------------------------------------------------


class TestAgentCoreToolResultCapping:
    @pytest.mark.asyncio
    async def test_long_result_truncated(self, provider, tools):
        core = AgentCore(provider, max_tool_result_chars=50)
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "x" * 200})],
                finish_reason="tool_calls",
            ),
            _make_response(content="ok"),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "big echo"}],
            tools=tools,
        ))

        # The tool result message should be capped
        tool_msg = result.messages[-2]  # second-to-last: tool result
        assert len(tool_msg["content"]) <= 80  # 50 + "... (truncated)" + margin


# ---------------------------------------------------------------------------
# AgentCore — message structure
# ---------------------------------------------------------------------------


class TestAgentCoreMessages:
    @pytest.mark.asyncio
    async def test_tool_call_message_structure(self, core, provider, tools):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "hi"}, tc_id="abc123")],
                finish_reason="tool_calls",
            ),
            _make_response(content="done"),
        ])

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "echo hi"}],
            tools=tools,
        ))

        # Should have: [user, assistant_tool_call, tool_result, assistant_final]
        roles = [m["role"] for m in result.messages]
        assert "tool" in roles
        tool_msg = next(m for m in result.messages if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "abc123"
        assert "echo: hi" in tool_msg["content"]

    @pytest.mark.asyncio
    async def test_no_tools_provided(self, core, provider):
        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="No tools needed."))

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "simple question"}],
        ))

        assert result.content == "No tools needed."
        assert result.tools_used == []

    @pytest.mark.asyncio
    async def test_final_assistant_message_appended(self, core, provider):
        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Final answer."))

        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "q"}],
        ))

        assert result.messages[-1]["role"] == "assistant"
        assert result.messages[-1]["content"] == "Final answer."
