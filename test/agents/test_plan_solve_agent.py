"""Tests for PlanSolveAgent."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.plan_solve_agent import PlanSolveAgent, _merge_usage
from core.runner import AgentCore, AgentInput, AgentOutput
from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tools import Tool, ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response(content: str = "", tool_calls=None, finish_reason="stop", usage=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=usage or {},
    )


def _tc(name: str, args: dict[str, Any], tc_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=tc_id, name=name, arguments=args)


class EchoTool(Tool):
    name = "echo"
    description = "Echo."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def execute(self, text: str = "") -> ToolResult:
        return ToolResult(success=True, content=f"echo: {text}")


# ---------------------------------------------------------------------------
# _merge_usage
# ---------------------------------------------------------------------------


class TestMergeUsage:
    def test_empty(self):
        assert _merge_usage({}, {}) == {}

    def test_sums_same_key(self):
        assert _merge_usage({"t": 10}, {"t": 5}) == {"t": 15}

    def test_disjoint_keys(self):
        assert _merge_usage({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPlanSolveHappy:
    @pytest.mark.asyncio
    async def test_plan_then_execute_no_tools(self):
        """Both phases succeed without tool calls."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Step one\n2. Step two"),
            _response(content="Executed all steps. Done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "complex task"}],
        ))

        assert result.content == "Executed all steps. Done."
        assert result.stop_reason == "stop"
        assert result.tools_used == []

    @pytest.mark.asyncio
    async def test_execution_uses_tools(self):
        """Execution phase calls tools."""
        tools = ToolRegistry()
        tools.register(EchoTool())

        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Echo something"),
            _response(
                tool_calls=[_tc("echo", {"text": "hello"})],
                finish_reason="tool_calls",
            ),
            _response(content="Done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "echo task"}],
            tools=tools,
        ))

        assert result.content == "Done."
        assert "echo" in result.tools_used

    @pytest.mark.asyncio
    async def test_planning_has_no_tools(self):
        """Verify the planning phase passes an empty tool list to the LLM."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Step"),
            _response(content="Done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        tools = ToolRegistry()
        tools.register(EchoTool())
        await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
            tools=tools,
        ))

        # First call (planning): tools should be empty list
        plan_call_args = provider.chat.call_args_list[0].kwargs
        assert plan_call_args["tools"] == []

        # Second call (execution): tools should have the echo tool
        exec_call_args = provider.chat.call_args_list[1].kwargs
        assert len(exec_call_args["tools"]) == 1

    @pytest.mark.asyncio
    async def test_execution_sees_plan(self):
        """Execution phase messages include the plan output."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Step alpha\n2. Step beta"),
            _response(content="Done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
        ))

        exec_messages = provider.chat.call_args_list[1].kwargs["messages"]
        # Find the plan content in execution messages
        plan_texts = [m["content"] for m in exec_messages if "Step alpha" in str(m.get("content", ""))]
        assert len(plan_texts) == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestPlanSolveErrors:
    @pytest.mark.asyncio
    async def test_planning_error_stops_early(self):
        """If planning fails, return immediately without execution."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=_response(
            content="Planning failed", finish_reason="error",
        ))
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
        ))

        assert result.stop_reason == "error"
        assert "Planning failed" in (result.error or "")
        # Only one LLM call (planning), no execution call
        assert provider.chat.call_count == 1


# ---------------------------------------------------------------------------
# Usage accumulation
# ---------------------------------------------------------------------------


class TestPlanSolveUsage:
    @pytest.mark.asyncio
    async def test_merges_usage_across_phases(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(
                content="## Plan\n1. Step",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
            _response(
                content="Done.",
                usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
            ),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
        ))

        assert result.usage["prompt_tokens"] == 300
        assert result.usage["completion_tokens"] == 130
        assert result.usage["total_tokens"] == 430


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------


class TestPlanSolveOutput:
    @pytest.mark.asyncio
    async def test_tool_events_from_both_phases(self):
        """Plan phase has no events; exec phase events are preserved."""
        tools = ToolRegistry()
        tools.register(EchoTool())

        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Echo x"),
            _response(
                tool_calls=[_tc("echo", {"text": "x"})],
                finish_reason="tool_calls",
            ),
            _response(content="Done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
            tools=tools,
        ))

        assert len(result.tool_events) == 1
        assert result.tool_events[0]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_final_message_is_assistant(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=[
            _response(content="## Plan\n1. Step"),
            _response(content="All done."),
        ])
        core = AgentCore(provider)
        agent = PlanSolveAgent(core)

        result = await agent.run(AgentInput(
            init_messages=[{"role": "user", "content": "task"}],
        ))

        assert result.messages[-1]["role"] == "assistant"
        assert result.messages[-1]["content"] == "All done."
