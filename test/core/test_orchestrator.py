"""Tests for Orchestrator — lifecycle, tools, skills delegation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.dispatcher import Dispatcher
from core.orchestrator import Orchestrator, OrchestratorResult
from core.runner import AgentInput, AgentOutput
from core.skills import SkillsLoader
from providers.base import LLMProvider, LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def provider():
    p = MagicMock(spec=LLMProvider)
    p.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    return p


@pytest.fixture
def orchestrator(workspace, provider):
    o = Orchestrator(
        workspace=workspace,
        provider=provider,
    )
    # Patch the auto-discovered react agent's run method
    react = o.dispatcher.agents["react"]
    react.run = AsyncMock(return_value=AgentOutput(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello!"},
        ],
        content="Hello!",
        usage={"total_tokens": 50},
        stop_reason="stop",
    ))
    return o


@pytest.fixture
def react_agent(orchestrator):
    """The auto-discovered react agent (already patched with mock run)."""
    return orchestrator.dispatcher.agents["react"]


# ---------------------------------------------------------------------------
# OrchestratorResult
# ---------------------------------------------------------------------------


class TestOrchestratorResult:
    def test_defaults(self):
        r = OrchestratorResult(content="ok", session_key="s1", paradigm="react")
        assert r.content == "ok"
        assert r.session_key == "s1"
        assert r.paradigm == "react"
        assert r.usage == {}
        assert r.stop_reason == "completed"
        assert r.error is None


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestInit:
    def test_auto_discovers_agents(self, workspace, provider):
        o = Orchestrator(workspace=workspace, provider=provider)
        assert "react" in o.dispatcher.agents
        assert "plan_solve" in o.dispatcher.agents
        assert isinstance(o.dispatcher, Dispatcher)
        assert o.context is not None

    def test_accepts_prebuilt_dispatcher(self, workspace, provider):
        from agents.react_agent import ReActAgent
        from core.runner import AgentCore

        agent = ReActAgent(AgentCore(provider))
        d = Dispatcher({"react": agent})
        o = Orchestrator(workspace=workspace, provider=provider, dispatcher=d)
        assert o.dispatcher is d

    def test_default_idle_compress_passed_to_context(self, workspace, provider):
        o = Orchestrator(workspace=workspace, provider=provider)
        assert o.context.idle_compress_seconds == 300

    def test_idle_compress_passed_to_context(self, workspace, provider):
        o = Orchestrator(
            workspace=workspace,
            provider=provider,
            idle_compress_seconds=60,
        )
        assert o.context.idle_compress_seconds == 60

    def test_idle_compress_disabled_passed_to_context(self, workspace, provider):
        o = Orchestrator(
            workspace=workspace,
            provider=provider,
            idle_compress_seconds=0,
        )
        assert o.context.idle_compress_seconds == 0


# ---------------------------------------------------------------------------
# run — happy path
# ---------------------------------------------------------------------------


class TestRunHappy:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, orchestrator):
        result = await orchestrator.process_message("s1", "hello")

        assert result.content == "Hello!"
        assert result.session_key == "s1"
        assert result.paradigm == "react"
        assert result.stop_reason == "stop"
        assert result.usage["total_tokens"] == 50
        assert result.error is None

    @pytest.mark.asyncio
    async def test_persists_session(self, orchestrator):
        await orchestrator.process_message("s2", "query")
        history = orchestrator.context.get_history("s2")
        assert len(history) >= 1
        # save_exchange saves the real user_input, then assistant responses
        assert history[0]["content"] == "query"
        assert history[-1]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_two_turn_conversation(self, orchestrator, react_agent):
        """Session history accumulates across turns."""
        async def echo_run(spec: AgentInput):
            msgs = list(spec.init_messages)
            msgs.append({"role": "assistant", "content": f"Reply to: {spec.init_messages[-1]['content']}"})
            return AgentOutput(messages=msgs, content=msgs[-1]["content"])
        react_agent.run = echo_run

        await orchestrator.process_message("s3", "first")
        await orchestrator.process_message("s3", "second")

        history = orchestrator.context.get_history("s3")
        contents = [m.get("content", "") for m in history if m.get("role") == "user"]
        assert "first" in contents
        assert "second" in contents

    @pytest.mark.asyncio
    async def test_passes_model_params(self, orchestrator, react_agent):
        await orchestrator.process_message("s4", "query", model="gpt-5", temperature=0.5, max_tokens=1000)
        spec: AgentInput = react_agent.run.call_args[0][0]
        assert spec.model == "gpt-5"
        assert spec.temperature == 0.5
        assert spec.max_tokens == 1000

    @pytest.mark.asyncio
    async def test_passes_goal(self, orchestrator, react_agent):
        await orchestrator.process_message("s5", "query", goal="Be concise.")
        spec: AgentInput = react_agent.run.call_args[0][0]
        assert spec.goal == "Be concise."

    @pytest.mark.asyncio
    async def test_empty_input_raises(self, orchestrator):
        with pytest.raises(ValueError, match="user_input"):
            await orchestrator.process_message("s6", "   ")


# ---------------------------------------------------------------------------
# run — keyboard interrupt
# ---------------------------------------------------------------------------


class TestRunKeyboardInterrupt:
    @pytest.mark.asyncio
    async def test_saves_partial_state(self, orchestrator, react_agent):
        react_agent.run.side_effect = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            await orchestrator.process_message("int1", "query")

        # Partial state should be saved
        session = orchestrator.context.session.get_session("int1")
        assert len(session.messages) >= 2  # system + user at minimum
        # Last message should be the interruption marker
        assert "interrupted" in str(session.messages[-1].get("content", "")).lower()


# ---------------------------------------------------------------------------
# run — error handling
# ---------------------------------------------------------------------------


class TestRunError:
    @pytest.mark.asyncio
    async def test_returns_error_result(self, orchestrator, react_agent):
        react_agent.run.side_effect = RuntimeError("Something went wrong")

        result = await orchestrator.process_message("err1", "query")

        assert result.stop_reason == "error"
        assert "Something went wrong" in (result.error or "")
        assert result.session_key == "err1"

    @pytest.mark.asyncio
    async def test_error_before_resolve(self, orchestrator, react_agent):
        """Error before paradigm resolution: paradigm='unknown'."""
        react_agent.run.side_effect = RuntimeError("early failure")
        orchestrator.ctx.session.get_session = MagicMock(
            side_effect=RuntimeError("session corrupted")
        )

        result = await orchestrator.process_message("err3", "query")
        assert result.stop_reason == "error"
        assert result.paradigm == "unknown"


# ---------------------------------------------------------------------------
# SkillsLoader
# ---------------------------------------------------------------------------


class TestSkillsLoader:
    @pytest.mark.asyncio
    async def test_loader_called(self, orchestrator, react_agent):
        from unittest.mock import MagicMock

        orchestrator.context.skills_loader.build_skills_summary = MagicMock(
            return_value="**web-search**: Search the web."
        )

        await orchestrator.process_message("sk1", "search for cats")

        spec: AgentInput = react_agent.run.call_args[0][0]
        system_msg = spec.init_messages[0]["content"]
        assert "web-search" in system_msg

    @pytest.mark.asyncio
    async def test_no_loader(self, orchestrator, react_agent):
        """Builtin skills are auto-discovered and injected."""
        await orchestrator.process_message("sk2", "query")
        spec: AgentInput = react_agent.run.call_args[0][0]
        system_msg = spec.init_messages[0]["content"]
        # Skills directory is populated → skills section is present
        assert "Available Skills" in system_msg

    @pytest.mark.asyncio
    async def test_explicit_skills_merged(self, orchestrator, react_agent):
        from unittest.mock import MagicMock

        orchestrator.context.skills_loader.build_skills_summary = MagicMock(
            return_value="**loaded-skill**: A loaded skill."
        )

        await orchestrator.process_message("sk3", "query", skills=["explicit-skill"])

        spec: AgentInput = react_agent.run.call_args[0][0]
        system_msg = spec.init_messages[0]["content"]
        assert "explicit-skill" in system_msg
        assert "loaded-skill" in system_msg

    @pytest.mark.asyncio
    async def test_loader_isinstance_check(self):
        """SkillsLoader is a concrete class from core.skills."""
        import tempfile
        from pathlib import Path

        from core.skills import SkillsLoader as ConcreteSkillsLoader

        with tempfile.TemporaryDirectory() as tmp:
            loader = ConcreteSkillsLoader(workspace=Path(tmp))
            assert isinstance(loader, SkillsLoader)

        class NotALoader:
            pass

        assert not isinstance(NotALoader(), SkillsLoader)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestTools:
    @pytest.mark.asyncio
    async def test_register_tool(self, orchestrator):
        from tools.tool import Tool, ToolResult

        class EchoTool(Tool):
            name = "echo"
            description = "Echo back."
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, content="")

        orchestrator.register_tool(EchoTool())
        assert "echo" in orchestrator.tools

    @pytest.mark.asyncio
    async def test_unregister_tool(self, orchestrator):
        from tools.tool import Tool, ToolResult

        class TempTool(Tool):
            name = "temp"
            description = "Temp."
            parameters = {}

            async def execute(self, **kwargs):
                return ToolResult(success=True, content="")

        orchestrator.register_tool(TempTool())
        orchestrator.unregister_tool("temp")
        assert "temp" not in orchestrator.tools

    @pytest.mark.asyncio
    async def test_tools_passed_to_spec(self, orchestrator, react_agent):
        from tools.tool import Tool, ToolResult

        class SearchTool(Tool):
            name = "search"
            description = "Search."
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, content="")

        orchestrator.register_tool(SearchTool())
        await orchestrator.process_message("t1", "query")

        spec: AgentInput = react_agent.run.call_args[0][0]
        assert "search" in spec.tools


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


class TestDelegation:
    def test_sessions(self, orchestrator):
        assert orchestrator.sessions == []

    def test_delete_session(self, orchestrator):
        orchestrator.context.save_session("del1", [{"role": "user", "content": "x"}])
        assert orchestrator.delete_session("del1") is True

    def test_remember_and_recall(self, orchestrator):
        orchestrator.remember("test-mem", "body", mem_type="user", description="desc")
        results = orchestrator.recall("body")
        assert len(results) >= 1

    def test_forget(self, orchestrator):
        orchestrator.remember("bye-mem", "x", mem_type="feedback", description="d")
        assert orchestrator.forget("bye-mem") is True
        assert orchestrator.forget("gone") is False
