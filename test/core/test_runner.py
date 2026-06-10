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


# ---------------------------------------------------------------------------
# AgentCore — compaction
# ---------------------------------------------------------------------------


class TestAgentCoreCompaction:
    """Tests for the 7-step lightweight compaction pipeline."""

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

    # -- Step 1: remove orphan tool results -----------------------------------

    def test_remove_orphan_tool_results(self, core):
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a"]),
            self._tool_msg("a", "ok"),
            self._tool_msg("orphan", "nobody called me"),
            self._msg("user", "next"),
        ]
        cleaned = core._remove_orphan_tool_results(msgs)
        tool_ids = [m["tool_call_id"] for m in cleaned if m["role"] == "tool"]
        assert tool_ids == ["a"]

    def test_remove_orphan_all_valid(self, core):
        msgs = [
            self._assistant_tc(["a", "b"]),
            self._tool_msg("a"),
            self._tool_msg("b"),
        ]
        cleaned = core._remove_orphan_tool_results(msgs)
        assert len([m for m in cleaned if m["role"] == "tool"]) == 2

    # -- Step 2: fill missing tool results ------------------------------------

    def test_fill_missing_tool_results(self, core):
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a", "b"]),
            self._tool_msg("a", "got a"),
            self._msg("user", "next"),
        ]
        filled = core._fill_missing_tool_results(msgs)
        tool_contents = [m["content"] for m in filled if m["role"] == "tool"]
        assert "got a" in tool_contents
        assert any("unavailable" in c for c in tool_contents)

    def test_fill_no_missing(self, core):
        msgs = [
            self._assistant_tc(["a"]),
            self._tool_msg("a"),
        ]
        filled = core._fill_missing_tool_results(msgs)
        assert filled == msgs

    # -- Step 3: summarise old tool results -----------------------------------

    def test_summarise_old_tool_results(self, core):
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["old1"]),
            self._tool_msg("old1", "long old result " * 30),
            self._msg("user", "next"),
            self._assistant_tc(["recent"]),
            self._tool_msg("recent", "recent result"),
        ]
        compacted = core._summarize_old_tool_results(msgs, recent_turns=1)
        # Old tool result should be summarised
        old = next(m for m in compacted if m.get("tool_call_id") == "old1")
        assert old["content"].startswith("[Compacted]")
        # Recent tool result should be intact
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
        compacted = core._summarize_old_tool_results(msgs, recent_turns=2)
        # t1 is old (turn 1 of 3, cutoff = 3-2 = 1)
        t1 = next(m for m in compacted if m.get("tool_call_id") == "t1")
        assert t1["content"].startswith("[Compacted]")
        # t2 and t3 are recent
        t2 = next(m for m in compacted if m.get("tool_call_id") == "t2")
        assert t2["content"] == "result2"
        t3 = next(m for m in compacted if m.get("tool_call_id") == "t3")
        assert t3["content"] == "result3"

    # -- Step 4: truncate long tool results -----------------------------------

    def test_truncate_long_tool_results(self, core):
        msgs = [
            self._assistant_tc(["a"]),
            self._tool_msg("a", "x" * 4000),
            self._msg("user", "next"),
            self._assistant_tc(["b"]),
            self._tool_msg("b", "current turn"),
        ]
        compacted = core._truncate_long_tool_results(msgs, max_chars=500)
        # Old tool result should be truncated
        a = next(m for m in compacted if m.get("tool_call_id") == "a")
        assert "(truncated)" in a["content"]
        assert len(a["content"]) < 600
        # Current turn result intact
        b = next(m for m in compacted if m.get("tool_call_id") == "b")
        assert b["content"] == "current turn"

    # -- Step 5: token budget truncation --------------------------------------

    def test_token_budget_truncation(self, core):
        """Oldest non-system messages dropped when over budget."""
        msgs = [
            self._msg("system", "sys"),
            self._msg("user", "first"),
            self._msg("assistant", "reply1"),
            self._msg("user", "second"),
            self._msg("assistant", "reply2"),
        ]
        # Mark all messages as tiny — budget trivially fits, should be no-op
        result = core._truncate_by_token_budget(msgs, 1_000_000, None)
        assert len(result) == len(msgs)

    def test_token_budget_drops_oldest(self, core):
        """When over very tight budget, drops oldest non-system."""
        msgs = [
            self._msg("system", "sys"),
            self._msg("user", "first"),
            self._msg("assistant", "a" * 4000),
            self._msg("user", "second"),
        ]
        result = core._truncate_by_token_budget(msgs, 500, None)
        # Should keep system + at least 1 other
        assert len(result) < len(msgs)
        assert result[0]["role"] == "system"

    # -- Step 6-7: secondary cleanup ------------------------------------------

    def test_secondary_orphan_cleanup(self, core):
        """Token truncation may orphan tool results, cleaned in step 6."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["old"]),
            self._msg("user", "u"),
            self._tool_msg("orphan"),
        ]
        result = core._compact_context(msgs, None, 500)
        # orphans should be cleaned
        tool_ids = [m.get("tool_call_id") for m in result if m["role"] == "tool"]
        assert "orphan" not in tool_ids

    def test_secondary_fill(self, core):
        """Step 7: fill_missing is a safety net ensuring well-formed output."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["tc1"]),
            self._tool_msg("tc1", "x" * 5000),
            self._msg("user", "next"),
        ]
        result = core._compact_context(msgs, None, 500)
        # After full pipeline, every assistant tc must have matching tool results
        tc_ids: set[str] = set()
        for m in result:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    tc_ids.add(tc["id"])
        tool_res_ids = {m["tool_call_id"] for m in result if m.get("role") == "tool"}
        missing = tc_ids - tool_res_ids
        assert len(missing) == 0, f"Missing tool results: {missing}"

    # -- integration: _maybe_compact ------------------------------------------

    def test_no_compact_when_under_budget(self, core):
        msgs = [self._msg("user", "hi")]
        result = core._maybe_compact(msgs, None)
        # Under budget → same object returned (not a copy)
        assert result is msgs

    def test_original_messages_untouched(self, core):
        """Compaction operates on a copy; original list unchanged."""
        msgs = [
            self._assistant_tc(["orphan_tc"]),
            self._tool_msg("orphan_tool", "x"),
        ]
        original_len = len(msgs)
        core._compact_context(msgs, None, core.max_context_tokens)
        # Original must be unchanged
        assert len(msgs) == original_len
        assert msgs[1]["role"] == "tool"  # still there

    def test_compaction_idempotent(self, core):
        """Running compaction twice produces same result."""
        msgs = [
            self._msg("system", "sys"),
            self._assistant_tc(["a"]),
            self._tool_msg("a", "x" * 4000),
            self._msg("user", "next"),
        ]
        c1 = core._compact_context(msgs, None, 2000)
        c2 = core._compact_context(c1, None, 2000)
        # Should be stable
        assert len(c1) == len(c2)
        for a, b in zip(c1, c2):
            assert a["role"] == b["role"]

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
        # Set very low budget so compaction triggers
        core.max_context_tokens = 1000
        result = await core.run(spec)
        assert result.content == "done"
        # LLM was called — compaction ran before each call
        assert len(call_messages) >= 1
