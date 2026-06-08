"""Tests for ContextManager — assembly, compression, repair, and memory integration."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from context.context_manager import (
    _INTERRUPT_MESSAGE,
    _INTERRUPT_TOOL_RESULT,
    ContextManager,
    _count_tokens,
    _estimate_message_tokens,
)
from providers.base import LLMProvider, LLMResponse
from tools import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def ctx(workspace):
    return ContextManager(workspace, system_prompt="You are a helpful assistant.")


@pytest.fixture
def provider():
    p = MagicMock(spec=LLMProvider)
    p.chat_with_retry = AsyncMock(return_value=LLMResponse(content=""))
    return p


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_count_tokens_string(self):
        n = _count_tokens("hello world")
        assert isinstance(n, int)
        assert n > 0

    def test_count_tokens_empty(self):
        assert _count_tokens("") == 0

    def test_estimate_messages(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
        ]
        total = _estimate_message_tokens(msgs)
        assert total > 0

    def test_estimate_with_tool_calls(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "search"}}]},
        ]
        total = _estimate_message_tokens(msgs)
        assert total > 0

    def test_estimate_content_list(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]},
        ]
        total = _estimate_message_tokens(msgs)
        assert total > 0


# ---------------------------------------------------------------------------
# ContextManager — init
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self, workspace):
        ctx = ContextManager(workspace)
        assert ctx.system_prompt == ""
        assert ctx.max_context_tokens == 128_000
        assert ctx.provider is None
        assert ctx.compress_model is None

    def test_custom_settings(self, workspace, provider):
        ctx = ContextManager(
            workspace,
            provider=provider,
            system_prompt="Be concise.",
            max_context_tokens=64_000,
            compress_model="gpt-4o-mini",
        )
        assert ctx.system_prompt == "Be concise."
        assert ctx.max_context_tokens == 64_000
        assert ctx.compress_model == "gpt-4o-mini"

    def test_creates_sub_managers(self, ctx):
        assert ctx.session is not None
        assert ctx.memory is not None


# ---------------------------------------------------------------------------
# ContextManager — build_messages (basic assembly)
# ---------------------------------------------------------------------------


class TestBuildMessages:
    def test_basic_assembly(self, ctx):
        msgs = ctx.build_messages("s1", "hello")
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "hello"

    def test_includes_system_prompt(self, ctx):
        msgs = ctx.build_messages("s2", "query")
        system = msgs[0]["content"]
        assert "You are a helpful assistant." in system

    def test_includes_session_history(self, ctx):
        # Simulate previous turn
        ctx.session.set_messages("s3", [
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ])
        msgs = ctx.build_messages("s3", "new question")
        # Should have: fresh system, user (previous), assistant, user (new)
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] != "old system"  # old system stripped
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "previous question"
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["content"] == "previous answer"
        assert msgs[3]["role"] == "user"
        assert msgs[3]["content"] == "new question"

    def test_strips_old_system_messages(self, ctx):
        ctx.session.set_messages("s4", [
            {"role": "system", "content": "old prompt"},
            {"role": "system", "content": "another old prompt"},
            {"role": "user", "content": "hi"},
        ])
        msgs = ctx.build_messages("s4", "again")
        # Only one system message (the fresh one)
        system_count = sum(1 for m in msgs if m["role"] == "system")
        assert system_count == 1
        assert "You are a helpful assistant." in msgs[0]["content"]


# ---------------------------------------------------------------------------
# ContextManager — build_messages (system prompt sources)
# ---------------------------------------------------------------------------


class TestSystemPromptAssembly:
    def test_memory_context_injected(self, ctx):
        ctx.remember("user-role", "I am a Python developer.",
                     mem_type="user", description="User role")
        msgs = ctx.build_messages("sp1", "query")
        system = msgs[0]["content"]
        assert "Python developer" in system

    def test_tool_descriptions_injected(self, ctx, workspace):
        from tools.tool import Tool, ToolResult

        class SearchTool(Tool):
            name = "search"
            description = "Search the web."
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, content="")

        tools = ToolRegistry()
        tools.register(SearchTool())
        msgs = ctx.build_messages("sp2", "query", tools=tools)
        system = msgs[0]["content"]
        assert "search" in system
        assert "Search the web" in system

    def test_skill_descriptions_injected(self, ctx):
        msgs = ctx.build_messages("sp3", "query", skills=["web-search", "code-review"])
        system = msgs[0]["content"]
        assert "web-search" in system
        assert "code-review" in system

    def test_no_tools_no_skills_section(self, ctx):
        msgs = ctx.build_messages("sp4", "query")
        system = msgs[0]["content"]
        assert "Available Tools" not in system
        assert "Available Skills" not in system

    def test_empty_system_prompt_ok(self, workspace):
        ctx = ContextManager(workspace)
        msgs = ctx.build_messages("sp5", "query")
        assert msgs[0]["role"] == "system"
        # May contain only memory sections (which are empty), or be minimal
        assert isinstance(msgs[0]["content"], str)


# ---------------------------------------------------------------------------
# ContextManager — session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_save_session_persists(self, ctx):
        ctx.save_session("life1", [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ])
        history = ctx.get_history("life1")
        assert len(history) == 2  # system stripped
        assert history[0]["content"] == "q"
        assert history[1]["content"] == "a"

    def test_save_then_build(self, ctx):
        """Simulate a full turn: build → save → next build includes history."""
        # Turn 1
        msgs1 = ctx.build_messages("turn", "first question")
        # Simulate agent response
        msgs1.append({"role": "assistant", "content": "first answer"})
        ctx.save_session("turn", msgs1)

        # Turn 2
        msgs2 = ctx.build_messages("turn", "second question")
        # Should include first question + first answer + second question
        contents = [m["content"] for m in msgs2 if m["role"] == "user"]
        assert "first question" in contents
        assert "second question" in contents

    def test_delete_session(self, ctx):
        ctx.save_session("del", [{"role": "user", "content": "x"}])
        assert ctx.delete_session("del") is True
        assert ctx.get_history("del") == []

    def test_list_sessions(self, ctx):
        ctx.save_session("a", [{"role": "user", "content": "a"}])
        ctx.save_session("b", [{"role": "user", "content": "b"}])
        sessions = ctx.list_sessions()
        keys = {s["key"] for s in sessions}
        assert "a" in keys
        assert "b" in keys


# ---------------------------------------------------------------------------
# ContextManager — compression
# ---------------------------------------------------------------------------


class TestCompression:
    def test_no_compression_when_under_budget(self, ctx):
        ctx.max_context_tokens = 1_000_000  # massive budget
        msgs = ctx.build_messages("c1", "short query")
        # No context-summary message injected
        for msg in msgs:
            assert "[Context summary" not in str(msg.get("content", ""))

    def test_compression_triggers_when_over_budget(self, ctx):
        ctx.max_context_tokens = 50  # tiny budget forces compression
        # Pre-populate session with older messages that will get compressed
        ctx.session.set_messages("c2", [
            {"role": "user", "content": "long history " + "x" * 200},
            {"role": "assistant", "content": "long response " + "y" * 200},
            {"role": "user", "content": "more history " + "z" * 200},
            {"role": "assistant", "content": "another response " + "w" * 200},
        ])
        msgs = ctx.build_messages("c2", "final question")
        # Should have a compressed summary message
        summaries = [
            m for m in msgs
            if "[Context summary" in str(m.get("content", ""))
        ]
        assert len(summaries) == 1

    def test_system_message_always_kept(self, ctx):
        ctx.max_context_tokens = 50
        msgs = ctx.build_messages("c3", "test")
        assert msgs[0]["role"] == "system"

    def test_recent_messages_kept(self, ctx):
        ctx.max_context_tokens = 100
        msgs = ctx.build_messages("c4", "final question")
        # The last message should always be the current input
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "final question"

    def test_truncation_summary_no_provider(self, ctx):
        """Without a provider, compression uses simple truncation."""
        ctx.max_context_tokens = 50
        # Build up some history first
        ctx.session.set_messages("c5", [
            {"role": "user", "content": "previous long conversation " + "x" * 200},
            {"role": "assistant", "content": "previous long answer " + "y" * 200},
        ])
        msgs = ctx.build_messages("c5", "new question")
        summaries = [
            m for m in msgs
            if "[Context summary" in str(m.get("content", ""))
        ]
        assert len(summaries) == 1
        assert "messages compressed" in summaries[0]["content"]

    def test_compression_disabled_when_max_zero(self, ctx):
        ctx.max_context_tokens = 0
        msgs = ctx.build_messages("c6", "query")
        # Should just proceed without compression
        assert msgs[-1]["content"] == "query"


# ---------------------------------------------------------------------------
# ContextManager — memory delegation
# ---------------------------------------------------------------------------


class TestMemoryDelegation:
    def test_remember(self, ctx):
        ctx.remember("mem1", "content", mem_type="user", description="desc")
        entry = ctx.memory.get("mem1")
        assert entry is not None
        assert entry.content == "content"

    def test_forget(self, ctx):
        ctx.remember("mem2", "content", mem_type="feedback", description="d")
        assert ctx.forget("mem2") is True
        assert ctx.forget("mem2") is False

    def test_recall(self, ctx):
        ctx.remember("python-tip", "Use list comprehensions.",
                     mem_type="user", description="Python tip")
        results = ctx.recall("python")
        assert len(results) >= 1

    def test_save_session_records_to_history(self, ctx):
        ctx.save_session("rec", [
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a programming language."},
        ])
        # Should have recorded to memory history
        assert ctx.memory.history_count >= 1


# ---------------------------------------------------------------------------
# ContextManager — interrupt repair
# ---------------------------------------------------------------------------


class TestRepair:
    """Tests for _repair_messages — unmatched message pair detection."""

    def test_repairs_unmatched_tool_calls(self):
        """Assistant with tool_calls but no matching tool results → synthetic results."""
        messages = [
            {"role": "user", "content": "search for cats"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "search", "arguments": "{}"}},
                {"id": "tc2", "function": {"name": "fetch", "arguments": "{}"}},
            ]},
        ]
        repaired, modified = ContextManager._repair_messages(messages)
        assert modified is True
        # Should have inserted two synthetic tool results
        tool_msgs = [m for m in repaired if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert tool_msgs[0]["tool_call_id"] == "tc1"
        assert tool_msgs[0]["content"] == _INTERRUPT_TOOL_RESULT
        assert tool_msgs[1]["tool_call_id"] == "tc2"

    def test_repairs_unmatched_last_user(self):
        """Last message is user → append synthetic assistant error."""
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "interrupted question"},
        ]
        repaired, modified = ContextManager._repair_messages(messages)
        assert modified is True
        assert repaired[-1]["role"] == "assistant"
        assert repaired[-1]["content"] == _INTERRUPT_MESSAGE

    def test_no_repair_when_paired(self):
        """Fully paired messages need no repair."""
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        repaired, modified = ContextManager._repair_messages(messages)
        assert modified is False
        assert repaired == messages

    def test_repairs_both_patterns(self):
        """Unmatched tool calls + last user message → both repaired."""
        messages = [
            {"role": "user", "content": "search for cats"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "search", "arguments": "{}"}},
            ]},
            # No tool result → interrupted
            {"role": "user", "content": "another question"},
            # No assistant response → interrupted again
        ]
        repaired, modified = ContextManager._repair_messages(messages)
        assert modified is True
        # Should have: user, assistant+tool_calls, synthetic tool result, user, synthetic assistant
        roles = [m["role"] for m in repaired]
        assert roles == ["user", "assistant", "tool", "user", "assistant"]
        assert repaired[2]["content"] == _INTERRUPT_TOOL_RESULT
        assert repaired[4]["content"] == _INTERRUPT_MESSAGE

    def test_no_repair_tool_result_exists(self):
        """Tool call with matching result → no repair needed for that pair."""
        messages = [
            {"role": "user", "content": "search"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "search", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "results found"},
            {"role": "assistant", "content": "Here are the results."},
            {"role": "user", "content": "next question"},
        ]
        repaired, modified = ContextManager._repair_messages(messages)
        # The tool call is matched, but the last user message has no response
        assert modified is True
        assert repaired[-1]["role"] == "assistant"
        assert repaired[-1]["content"] == _INTERRUPT_MESSAGE

    def test_empty_messages(self):
        """Empty message list needs no repair."""
        repaired, modified = ContextManager._repair_messages([])
        assert modified is False
        assert repaired == []

    def test_repair_session_integration(self, ctx):
        """_repair_session saves repaired messages back."""
        session = ctx.session.get_session("repair-int")
        session.messages = [
            {"role": "user", "content": "interrupted question"},
        ]
        ctx.session.save_session(session)

        # build_messages triggers _repair_session
        ctx.build_messages("repair-int", "new question")

        # Session should now have the repair
        session_after = ctx.session.get_session("repair-int")
        roles = [m["role"] for m in session_after.messages]
        assert "assistant" in roles  # synthetic assistant added


# ---------------------------------------------------------------------------
# ContextManager — idle compression
# ---------------------------------------------------------------------------


class TestIdleCompression:
    def test_no_compression_when_under_threshold(self, ctx):
        """Session updated just now → no compression."""
        ctx.idle_compress_seconds = 60
        session = ctx.session.get_session("idle1")
        session.messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]
        session.updated_at = datetime.now()
        ctx.session.save_session(session)

        msgs = ctx.build_messages("idle1", "new question")

        # No summary should be injected
        contents = [m.get("content", "") for m in msgs]
        assert not any("Session summary" in str(c) for c in contents)

    def test_compression_triggers_when_stale(self, ctx, provider):
        """Session idle for a long time → older messages summarised."""
        ctx.idle_compress_seconds = 1
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary of old conversation."))

        session = ctx.session.get_session("idle2")
        session.messages = [
            {"role": "system", "content": "old sys"},
            {"role": "user", "content": "old q1"},
            {"role": "assistant", "content": "old a1"},
            {"role": "user", "content": "old q2"},
            {"role": "assistant", "content": "old a2"},
            {"role": "user", "content": "old q3"},
            {"role": "assistant", "content": "old a3"},
        ]
        session.updated_at = datetime.now() - timedelta(hours=1)
        ctx.session.save_session(session)

        ctx.build_messages("idle2", "new question")

        # Provider should have been called for summarisation
        provider.chat_with_retry.assert_called_once()

        # The summary should be in the session
        session_after = ctx.session.get_session("idle2")
        contents = [m.get("content", "") for m in session_after.messages]
        assert any("Session summary" in str(c) for c in contents)

    def test_no_compression_when_disabled(self, ctx):
        """idle_compress_seconds=0 disables idle compression."""
        ctx.idle_compress_seconds = 0

        session = ctx.session.get_session("idle3")
        session.messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ] * 5
        session.updated_at = datetime.now() - timedelta(days=1)
        ctx.session.save_session(session)

        ctx.build_messages("idle3", "new question")

        session_after = ctx.session.get_session("idle3")
        contents = [m.get("content", "") for m in session_after.messages]
        assert not any("Session summary" in str(c) for c in contents)

    def test_compression_respects_consolidated_cursor(self, ctx):
        """Already-compressed messages are not re-compressed."""
        ctx.idle_compress_seconds = 1

        session = ctx.session.get_session("idle4")
        session.messages = [
            {"role": "user", "content": "[Session summary]\nAlready summarised."},
            {"role": "user", "content": "recent q1"},
            {"role": "assistant", "content": "recent a1"},
        ]
        session.consolidated_cursor = 1
        session.updated_at = datetime.now() - timedelta(hours=1)
        ctx.session.save_session(session)

        ctx.build_messages("idle4", "another question")

        # Only 2 messages after cursor, keep_recent=4, so nothing should change
        session_after = ctx.session.get_session("idle4")
        assert session_after.consolidated_cursor == 1

    def test_truncation_fallback_no_provider(self, ctx):
        """Without provider, compression falls back to truncation."""
        ctx.idle_compress_seconds = 1

        session = ctx.session.get_session("idle5")
        session.messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
            {"role": "user", "content": "c" * 100},
            {"role": "assistant", "content": "d" * 100},
            {"role": "user", "content": "e" * 100},
            {"role": "assistant", "content": "f" * 100},
        ]
        session.updated_at = datetime.now() - timedelta(hours=2)
        ctx.session.save_session(session)

        ctx.build_messages("idle5", "final")

        # Should have compressed with truncation fallback
        session_after = ctx.session.get_session("idle5")
        contents = [m.get("content", "") for m in session_after.messages]
        assert any("messages compressed" in str(c) for c in contents)

    def test_idle_compress_handles_summarise_failure(self, ctx):
        """When LLM summarisation fails, falls back to truncation."""
        ctx.idle_compress_seconds = 1
        ctx.provider = MagicMock(spec=LLMProvider)
        ctx.provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        session = ctx.session.get_session("idle6")
        session.messages = [
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "y" * 100},
            {"role": "user", "content": "z" * 100},
            {"role": "assistant", "content": "w" * 100},
            {"role": "user", "content": "v" * 100},
            {"role": "assistant", "content": "u" * 100},
        ]
        session.updated_at = datetime.now() - timedelta(hours=1)
        ctx.session.save_session(session)

        ctx.build_messages("idle6", "final")
        session_after = ctx.session.get_session("idle6")
        contents = [m.get("content", "") for m in session_after.messages]
        assert any("messages compressed" in str(c) for c in contents)
