"""Tests for BaseAgent and ReActAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.react_agent import ReActAgent
from core.agent_base import BaseAgent
from core.runner import AgentCore, AgentInput, AgentOutput
from tools import ToolRegistry

# ---------------------------------------------------------------------------
# BaseAgent — abstract enforcement
# ---------------------------------------------------------------------------


class TestBaseAgentAbstract:
    def test_cannot_instantiate_directly(self):
        core = MagicMock(spec=AgentCore)
        with pytest.raises(TypeError, match="abstract"):
            BaseAgent(core)  # type: ignore[abstract]

    def test_concrete_subclass_ok(self):
        core = MagicMock(spec=AgentCore)

        class _Concrete(BaseAgent):
            paradigm = "test"
            async def run(self, spec: AgentInput) -> AgentOutput:
                return AgentOutput(content="done")

        agent = _Concrete(core)
        assert agent.paradigm == "test"


# ---------------------------------------------------------------------------
# BaseAgent — message helpers
# ---------------------------------------------------------------------------


class TestBaseAgentMessages:
    def test_user(self):
        assert BaseAgent._user("hi") == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# BaseAgent — _with_spec
# ---------------------------------------------------------------------------


class TestWithSpec:
    def test_returns_copy_with_overrides(self):
        tools = ToolRegistry()
        original = AgentInput(
            init_messages=[{"role": "user", "content": "q"}],
            tools=tools,
            goal="test goal",
            model="gpt-5",
        )
        modified = BaseAgent._with_spec(original, goal="new goal", model=None)

        # Modified fields
        assert modified.goal == "new goal"
        assert modified.model is None
        # Unchanged fields
        assert modified.init_messages == original.init_messages
        assert modified.tools is original.tools

    def test_original_unchanged(self):
        original = AgentInput(goal="original", model="m1")
        BaseAgent._with_spec(original, goal="changed")
        assert original.goal == "original"


# ---------------------------------------------------------------------------
# ReActAgent
# ---------------------------------------------------------------------------


class TestReActAgent:
    @pytest.mark.asyncio
    async def test_paradigm_name(self):
        agent = ReActAgent(MagicMock(spec=AgentCore))
        assert agent.paradigm == "react"

    @pytest.mark.asyncio
    async def test_delegates_to_core(self):
        core = MagicMock(spec=AgentCore)
        expected = AgentOutput(content="response")
        core.run = AsyncMock(return_value=expected)

        agent = ReActAgent(core)
        spec = AgentInput(init_messages=[{"role": "user", "content": "hi"}])
        result = await agent.run(spec)

        core.run.assert_awaited_once_with(spec)
        assert result is expected
