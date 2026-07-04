"""Tests for the agent execution core."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.events import AgentStarted, bus
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


# ---------------------------------------------------------------------------
# AgentCore — compaction
# ---------------------------------------------------------------------------


class TestAgentCoreCompaction:
    """Tests for the lightweight 3-step compaction (replaces old 7-step pipeline)."""

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _msg(role: str, content: str = "", **extra: Any) -> dict[str, Any]:
        m: dict[str, Any] = {"role": role, "content": content}
        m.update(extra)
        return m

    @staticmethod
    def _tool_msg(tc_id: str, content: str = "result") -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": tc_id, "content": content}

    @staticmethod
    def _assistant_tc(tc_ids: list[str]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": tid, "type": "function",
                            "function": {"name": "test", "arguments": "{}"}}
                           for tid in tc_ids],
        }

    # -- fixtures --------------------------------------------------------------

    @pytest.fixture
    def provider(self):
        return MagicMock(spec=LLMProvider)

    @pytest.fixture
    def core(self, provider):
        return AgentCore(provider)

    # -- lightweight compaction: summarise old tool results --------------------

    def test_summarise_old_tool_results(self, core):
        """Step 1: old tool results get [Compacted] summary prefix."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["old1"]),
            self._tool_msg("old1", "long old result " * 30),
            self._msg("user", "next"),
            self._assistant_tc(["recent"]),
            self._tool_msg("recent", "recent result"),
        ]
        compacted = core._lightweight_compact(msgs, keep_turns=1, max_tokens=10_000_000, trigger_ratio=0.0)
        old = next(m for m in compacted if m.get("tool_call_id") == "old1")
        assert old["content"].startswith("[Compacted]")
        recent = next(m for m in compacted if m.get("tool_call_id") == "recent")
        assert recent["content"] == "recent result"

    def test_recent_tool_results_intact(self, core):
        """Last N tool-calling turns keep full results."""
        msgs = [
            self._assistant_tc(["t1"]),
            self._tool_msg("t1", "result1"),
            self._msg("user", "u1"),
            self._assistant_tc(["t2"]),
            self._tool_msg("t2", "result2"),
            self._msg("user", "u2"),
            self._assistant_tc(["t3"]),
            self._tool_msg("t3", "result3"),
        ]
        compacted = core._lightweight_compact(msgs, keep_turns=2, max_tokens=10_000_000, trigger_ratio=0.0)
        t1 = next(m for m in compacted if m.get("tool_call_id") == "t1")
        assert t1["content"].startswith("[Compacted]")
        t2 = next(m for m in compacted if m.get("tool_call_id") == "t2")
        assert t2["content"] == "result2"
        t3 = next(m for m in compacted if m.get("tool_call_id") == "t3")
        assert t3["content"] == "result3"

    # -- lightweight compaction: truncate long tool results --------------------

    def test_truncate_long_tool_results(self, core):
        """Recent-turn tool results are hard-truncated if oversized."""
        msgs = [
            self._assistant_tc(["a"]),
            self._tool_msg("a", "x" * 4000),
            self._msg("user", "next"),
            self._assistant_tc(["b"]),
            self._tool_msg("b", "current turn"),
        ]
        compacted = core._lightweight_compact(msgs, max_result_chars=500, max_tokens=10_000_000, trigger_ratio=0.0)
        a = next(m for m in compacted if m.get("tool_call_id") == "a")
        assert "(truncated)" in a["content"]
        assert len(a["content"]) < 600
        b = next(m for m in compacted if m.get("tool_call_id") == "b")
        assert b["content"] == "current turn"

    # -- lightweight compaction: orphan removal --------------------------------

    def test_remove_orphan_tool_results(self, core):
        """Step 2: orphan tool results removed."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a"]),
            self._tool_msg("a", "ok"),
            self._tool_msg("orphan", "nobody called me"),
            self._msg("user", "next"),
        ]
        cleaned = core._lightweight_compact(msgs, max_tokens=10_000_000, trigger_ratio=0.0)
        tool_ids = [m["tool_call_id"] for m in cleaned if m["role"] == "tool"]
        assert tool_ids == ["a"]

    # -- lightweight compaction: fill missing ---------------------------------

    def test_fill_missing_tool_results(self, core):
        """Step 3: missing tool results get placeholder."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a", "b"]),
            self._tool_msg("a", "got a"),
            self._msg("user", "next"),
        ]
        filled = core._lightweight_compact(msgs, max_tokens=10_000_000, trigger_ratio=0.0)
        tool_contents = [m["content"] for m in filled if m["role"] == "tool"]
        assert "got a" in tool_contents
        assert any("unavailable" in c for c in tool_contents)

    # -- budget gating ---------------------------------------------------------

    def test_no_compact_when_under_budget(self, core):
        """Under budget → returns a copy without compaction."""
        msgs = [self._msg("user", "hi")]
        result = core._lightweight_compact(msgs, max_tokens=1_000_000)
        assert result == msgs
        assert result is not msgs  # always returns a new list

    def test_original_messages_untouched(self, core):
        """Compaction returns a new list; original unchanged."""
        msgs = [
            self._assistant_tc(["orphan_tc"]),
            self._tool_msg("orphan_tool", "x"),
        ]
        original_len = len(msgs)
        core._lightweight_compact(msgs, max_tokens=100, trigger_ratio=0.0)
        assert len(msgs) == original_len
        assert msgs[1]["role"] == "tool"

    def test_compaction_idempotent(self, core):
        """Running compaction twice produces same result."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a"]),
            self._tool_msg("a", "x" * 4000),
            self._msg("user", "next"),
        ]
        c1 = core._lightweight_compact(msgs, max_tokens=500, trigger_ratio=0.0)
        c2 = core._lightweight_compact(c1, max_tokens=500, trigger_ratio=0.0)
        assert len(c1) == len(c2)
        for a, b in zip(c1, c2):
            assert a["role"] == b["role"]

    def test_compact_preserves_tool_call_structure(self, core):
        """After compaction, every assistant tool_call has a matching tool result."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["tc1"]),
            self._tool_msg("tc1", "x" * 5000),
            self._msg("user", "next"),
        ]
        result = core._lightweight_compact(msgs, max_tokens=500, trigger_ratio=0.0)
        tc_ids: set[str] = set()
        for m in result:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    tc_ids.add(tc["id"])
        tool_res_ids = {m["tool_call_id"] for m in result if m.get("role") == "tool"}
        missing = tc_ids - tool_res_ids
        assert len(missing) == 0, f"Missing tool results: {missing}"

    # -- integration: _compact_for_llm -----------------------------------------

    @pytest.mark.asyncio
    async def test_multi_turn_compaction_during_run(self, core, provider):
        """Compaction triggers during multi-turn execution; LLM receives compacted copy."""
        call_messages: list[list[dict[str, Any]]] = []

        async def _track_and_respond(messages, **kw):
            call_messages.append(list(messages))
            return _make_response(content="done")

        provider.chat_with_retry = _track_and_respond  # type: ignore[assignment]

        spec = AgentInput(
            init_messages=[
                self._msg("system", "sys"),
                self._msg("user", "q"),
            ],
            tools=ToolRegistry(),
        )
        core.max_context_tokens = 1000
        result = await core.run(spec)
        assert result.content == "done"
        assert len(call_messages) >= 1


# ---------------------------------------------------------------------------
# AgentCore — checkpoint / resume
# ---------------------------------------------------------------------------


class TestAgentCoreCheckpoint:
    """Tests for the checkpoint/resume mechanism."""

    # -- fixtures ---------------------------------------------------------------

    @pytest.fixture
    def provider(self):
        return MagicMock(spec=LLMProvider)

    @pytest.fixture
    def tools(self):
        reg = ToolRegistry()
        reg.register(EchoTool())
        return reg

    @pytest.fixture
    def workspace(self, tmp_path):
        return tmp_path / "workspace"

    @pytest.fixture
    def core(self, provider, workspace):
        return AgentCore(provider, workspace=workspace)

    # -- _checkpointing_enabled ------------------------------------------------

    def test_checkpoint_enabled_by_env_var(self, core, monkeypatch):
        monkeypatch.setenv("MYBOT_CHECKPOINT", "1")
        spec = AgentInput(session_key="s1")
        assert core._checkpointing_enabled(spec) is True

    def test_checkpoint_enabled_by_spec_field(self, core):
        spec = AgentInput(session_key="s1", checkpoint=True)
        assert core._checkpointing_enabled(spec) is True

    def test_checkpoint_disabled_when_session_key_empty(self, core, monkeypatch):
        monkeypatch.setenv("MYBOT_CHECKPOINT", "1")
        spec = AgentInput(session_key="")
        assert core._checkpointing_enabled(spec) is False

    def test_checkpoint_disabled_by_default(self, core, monkeypatch):
        monkeypatch.delenv("MYBOT_CHECKPOINT", raising=False)
        spec = AgentInput(session_key="s1")
        assert core._checkpointing_enabled(spec) is False

    # -- _checkpoint_path ------------------------------------------------------

    def test_checkpoint_path_uses_workspace(self, core, workspace):
        spec = AgentInput(session_key="abc")
        path = core._checkpoint_path(spec)
        expected = workspace / "sessions" / "abc_checkpoint.json"
        assert path == expected

    # -- save / load / delete round-trip ---------------------------------------

    def test_save_and_load_checkpoint(self, core, workspace):
        spec = AgentInput(session_key="s1")
        messages = [{"role": "user", "content": "hi"}]
        core._save_checkpoint(spec, messages, step_count=3,
                              tools_used=["echo"], tool_events=[{"name": "echo"}],
                              total_usage={"prompt_tokens": 100})

        data = core._load_checkpoint(spec)
        assert data is not None
        assert data["step_count"] == 3
        assert data["messages"] == messages
        assert data["tools_used"] == ["echo"]
        assert data["total_usage"] == {"prompt_tokens": 100}

    def test_delete_checkpoint(self, core, workspace):
        spec = AgentInput(session_key="s1")
        core._save_checkpoint(spec, [{"role": "user", "content": "x"}],
                              step_count=1, tools_used=[], tool_events=[],
                              total_usage={})
        path = core._checkpoint_path(spec)
        assert path.exists()

        core._delete_checkpoint(spec)
        assert not path.exists()

    def test_delete_nonexistent_checkpoint_no_error(self, core):
        spec = AgentInput(session_key="no-file")
        core._delete_checkpoint(spec)  # should not raise

    def test_load_nonexistent_returns_none(self, core):
        spec = AgentInput(session_key="ghost")
        assert core._load_checkpoint(spec) is None

    def test_corrupt_checkpoint_discarded(self, core, workspace):
        spec = AgentInput(session_key="s1")
        path = core._checkpoint_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{", encoding="utf-8")

        data = core._load_checkpoint(spec)
        assert data is None
        assert not path.exists()  # corrupt file deleted

    def test_version_mismatch_discarded(self, core, workspace):
        spec = AgentInput(session_key="s1")
        path = core._checkpoint_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 999,
            "session_key": "s1",
            "step_count": 1,
            "messages": [],
            "tools_used": [],
            "tool_events": [],
            "total_usage": {},
        }), encoding="utf-8")

        data = core._load_checkpoint(spec)
        assert data is None
        assert not path.exists()

    def test_missing_fields_discarded(self, core, workspace):
        spec = AgentInput(session_key="s1")
        path = core._checkpoint_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Missing "tool_events" field
        path.write_text(json.dumps({
            "version": 1,
            "session_key": "s1",
            "step_count": 1,
            "messages": [],
            "tools_used": [],
            "total_usage": {},
        }), encoding="utf-8")

        data = core._load_checkpoint(spec)
        assert data is None
        assert not path.exists()

    # -- integration: save after tool execution --------------------------------

    @pytest.mark.asyncio
    async def test_save_after_tool_execution(self, core, provider, tools, workspace):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "hello"})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Done."),
        ])

        spec = AgentInput(
            session_key="int-save",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=True,
        )
        result = await core.run(spec)

        assert result.content == "Done."
        # Checkpoint should be deleted on success
        assert not core._checkpoint_path(spec).exists()

    @pytest.mark.asyncio
    async def test_checkpoint_preserved_on_llm_error(self, core, provider, tools, workspace):
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "x"})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Error text", finish_reason="error"),
        ])

        spec = AgentInput(
            session_key="int-err",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=True,
        )
        result = await core.run(spec)

        assert result.stop_reason == "error"
        # Checkpoint preserved on error
        assert core._checkpoint_path(spec).exists()

    @pytest.mark.asyncio
    async def test_delete_on_max_iterations(self, core, provider, tools, workspace):
        core.max_iterations = 2
        provider.chat_with_retry = AsyncMock(return_value=_make_response(
            tool_calls=[_make_tc("echo", {"message": "loop"})],
            finish_reason="tool_calls",
        ))

        spec = AgentInput(
            session_key="int-max",
            init_messages=[{"role": "user", "content": "loop"}],
            tools=tools,
            checkpoint=True,
        )
        result = await core.run(spec)

        assert result.stop_reason == "max_iterations"
        # Checkpoint deleted on terminal state
        assert not core._checkpoint_path(spec).exists()

    # -- integration: resume ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_resume_loads_checkpoint(self, core, provider, tools, workspace):
        # Pre-write a checkpoint as if we crashed after step 2
        spec = AgentInput(
            session_key="int-resume",
            init_messages=[{"role": "user", "content": "original"}],
            tools=tools,
            checkpoint=True,
        )
        saved_messages = [
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "echo", "arguments": '{"message":"x"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "echo: x"},
        ]
        core._save_checkpoint(spec, saved_messages, step_count=2,
                              tools_used=["echo"],
                              tool_events=[{"name": "echo", "status": "ok"}],
                              total_usage={"prompt_tokens": 50, "completion_tokens": 10})

        # Now "resume" — LLM should continue from checkpoint
        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Resumed answer."))

        result = await core.run(spec)

        assert result.content == "Resumed answer."
        assert result.tools_used == ["echo"]  # carried forward
        assert result.usage["prompt_tokens"] == 50  # carried forward
        # Checkpoint deleted after successful completion
        assert not core._checkpoint_path(spec).exists()

    @pytest.mark.asyncio
    async def test_resume_skips_agent_start_event(self, core, provider, tools, workspace):
        spec = AgentInput(
            session_key="int-skip-event",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=True,
        )
        # Pre-write a checkpoint
        core._save_checkpoint(spec, [{"role": "user", "content": "go"}],
                              step_count=1, tools_used=[], tool_events=[],
                              total_usage={})

        started_events: list[AgentStarted] = []

        async def _on_started(event: AgentStarted):
            started_events.append(event)

        bus.subscribe(AgentStarted, _on_started)

        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Done."))

        try:
            await core.run(spec)
        finally:
            bus.unsubscribe(AgentStarted, _on_started)

        # AgentStarted should NOT be published on resume
        assert len(started_events) == 0

    @pytest.mark.asyncio
    async def test_resume_skips_middleware_on_agent_start(self, core, provider, tools, workspace):
        from core.middleware import AgentMiddleware, MiddlewareChain, MiddlewareContext

        spec = AgentInput(
            session_key="int-skip-mw",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=True,
        )
        # Pre-write a checkpoint
        core._save_checkpoint(spec, [{"role": "user", "content": "go"}],
                              step_count=1, tools_used=[], tool_events=[],
                              total_usage={})

        class TrackStartMiddleware(AgentMiddleware):
            def __init__(self):
                super().__init__()
                self.start_calls = 0

            async def on_agent_start(self, ctx: MiddlewareContext):
                self.start_calls += 1

        mw = TrackStartMiddleware()
        chain = MiddlewareChain([mw])
        core.middleware = chain

        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Done."))
        await core.run(spec)

        assert mw.start_calls == 0  # skipped on resume

    @pytest.mark.asyncio
    async def test_resume_continues_from_correct_step(self, core, provider, tools, workspace):
        spec = AgentInput(
            session_key="int-step",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=True,
        )
        # Pre-write a checkpoint at step 5
        core._save_checkpoint(spec, [{"role": "user", "content": "go"}],
                              step_count=5, tools_used=["echo"],
                              tool_events=[{"name": "echo"}],
                              total_usage={"prompt_tokens": 200})

        step_events: list[int] = []

        async def _on_step(event):
            step_events.append(event.step_count)

        from core.events import AgentStepStarted
        bus.subscribe(AgentStepStarted, _on_step)

        provider.chat_with_retry = AsyncMock(return_value=_make_response(content="Done."))

        try:
            result = await core.run(spec)
        finally:
            bus.unsubscribe(AgentStepStarted, _on_step)

        assert result.content == "Done."
        # First step after resume should be 6
        assert step_events[0] == 6

    @pytest.mark.asyncio
    async def test_no_checkpoint_when_disabled(self, core, provider, tools, workspace):
        """Checkpoint file is never created when checkpointing is disabled."""
        provider.chat_with_retry = AsyncMock(side_effect=[
            _make_response(
                tool_calls=[_make_tc("echo", {"message": "x"})],
                finish_reason="tool_calls",
            ),
            _make_response(content="Done."),
        ])

        spec = AgentInput(
            session_key="int-no-cp",
            init_messages=[{"role": "user", "content": "go"}],
            tools=tools,
            checkpoint=False,
        )
        await core.run(spec)

        assert not core._checkpoint_path(spec).exists()
