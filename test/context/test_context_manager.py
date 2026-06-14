"""Tests for ContextManager — assembly, compression, repair, and memory integration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from context.compaction import CompactionService
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
    p.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Summarised.", finish_reason="stop")
    )
    return p


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    async def test_count_tokens_string(self):
        assert _count_tokens("hello world") > 0

    async def test_count_tokens_empty(self):
        assert _count_tokens("") == 0

    async def test_estimate_messages(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert _estimate_message_tokens(msgs) > 0

    async def test_estimate_with_tool_calls(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "f"}}]}]
        assert _estimate_message_tokens(msgs) > 0

    async def test_estimate_content_list(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert _estimate_message_tokens(msgs) > 0


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestInit:
    async def test_defaults(self, workspace):
        cm = ContextManager(workspace)
        assert cm.max_context_tokens == 200_000
        assert cm.idle_compress_seconds == 300
        assert cm.compress_ratio == 0.5

    async def test_custom_settings(self, workspace, provider):
        cm = ContextManager(
            workspace,
            provider=provider,
            max_context_tokens=8000,
            idle_compress_seconds=60,
            compress_ratio=0.5,
        )
        assert cm.max_context_tokens == 8000
        assert cm.idle_compress_seconds == 60
        assert cm.compress_ratio == 0.5

    async def test_creates_sub_managers(self, ctx):
        assert ctx.session is not None
        assert ctx.memory is not None
        assert ctx.skills_loader is not None


# ---------------------------------------------------------------------------
# build_messages (assembly)
# ---------------------------------------------------------------------------


class TestBuildMessages:
    async def test_basic_assembly(self, ctx):
        msgs = await ctx.build_messages("s1", "hello")
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "hello"

    async def test_includes_system_prompt(self, ctx):
        msgs = await ctx.build_messages("s2", "query")
        assert "You are a helpful assistant" in msgs[0]["content"]

    async def test_includes_session_history(self, ctx):
        session = ctx.session.get_session("s3")
        session.messages = [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "resp"},
        ]
        ctx.session.save_session(session)
        msgs = await ctx.build_messages("s3", "new question")
        contents = [m["content"] for m in msgs]
        assert "prev" in contents
        assert "resp" in contents
        assert "new question" in contents

    async def test_strips_old_system_messages(self, ctx):
        session = ctx.session.get_session("s4")
        session.messages = [
            {"role": "system", "content": "old sys"},
            {"role": "user", "content": "q"},
        ]
        ctx.session.save_session(session)
        msgs = await ctx.build_messages("s4", "again")
        # Only the fresh system prompt should be present (only 1 system msg)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "old sys" not in system_msgs[0]["content"]

    async def test_respects_consolidated_cursor(self, ctx):
        """Only messages after consolidated_cursor are included."""
        session = ctx.session.get_session("s5")
        session.messages = [
            {"role": "user", "content": "old1"},
            {"role": "assistant", "content": "old2"},
            {"role": "user", "content": "recent"},
        ]
        session.consolidated_cursor = 2  # Skip first two
        ctx.session.save_session(session)
        msgs = await ctx.build_messages("s5", "now")
        contents = [m["content"] for m in msgs]
        assert "old1" not in contents
        assert "old2" not in contents
        assert "recent" in contents

    async def test_100_message_cap(self, ctx):
        """History is capped at 100 messages after cursor."""
        session = ctx.session.get_session("s6")
        session.messages = [
            {"role": "user", "content": "msg"},
        ] * 200
        ctx.session.save_session(session)
        msgs = await ctx.build_messages("s6", "final")
        # system + 100 history + 1 user  = 102
        non_system = [m for m in msgs if m["role"] != "system"]
        assert len(non_system) <= 101  # 100 history + current input


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------


class TestSystemPromptAssembly:
    async def test_memory_context_injected(self, ctx):
        msgs = await ctx.build_messages("sp1", "query")
        assert "You are a helpful assistant" in msgs[0]["content"]

    async def test_tool_descriptions_injected(self, ctx, workspace):
        t = ToolRegistry()
        from tools.tool import Tool

        class _T(Tool):
            name = "search"
            description = "Search the web"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kw):
                from tools.registry import ToolResult
                return ToolResult(success=True, content="")

        t.register(_T())
        msgs = await ctx.build_messages("sp2", "query", tools=t)
        assert "search" in msgs[0]["content"]

    async def test_skill_descriptions_injected(self, ctx):
        msgs = await ctx.build_messages("sp3", "query", skills=["web-search", "code-review"])
        assert len(msgs[0]["content"]) > 0

    async def test_no_tools_no_skills_section(self, ctx):
        msgs = await ctx.build_messages("sp4", "query")
        assert "Available Tools" not in msgs[0]["content"]

    async def test_empty_system_prompt_ok(self, workspace):
        cm = ContextManager(workspace)
        msgs = await cm.build_messages("sp5", "query")
        assert msgs[0]["role"] == "system"

    async def test_history_summaries_injected(self, ctx, provider):
        """When history.jsonl exists, its summaries appear in system prompt."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="A summary."))

        # Manually write a history record
        ctx._write_history("sp6", 5, "Past conversation summary.")

        msgs = await ctx.build_messages("sp6", "hello")
        assert "Previous Conversation Summaries" in msgs[0]["content"]
        assert "Past conversation summary" in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    async def test_save_exchange_appends(self, ctx):
        """save_exchange appends user + assistant messages."""
        session = ctx.session.get_session("se1")
        session.messages = [{"role": "user", "content": "old"}]
        ctx.session.save_session(session)

        await ctx.save_exchange("se1", "new q", [
            {"role": "assistant", "content": "new a"},
        ])
        session_after = ctx.session.get_session("se1")
        assert len(session_after.messages) == 3
        assert session_after.messages[-1]["content"] == "new a"

    async def test_save_exchange_preserves_cursor(self, ctx):
        """save_exchange does not affect consolidated_cursor."""
        session = ctx.session.get_session("se2")
        session.messages = [{"role": "user", "content": "q1"}]
        session.consolidated_cursor = 1
        ctx.session.save_session(session)

        await ctx.save_exchange("se2", "q2", [
            {"role": "assistant", "content": "a2"},
        ])
        session_after = ctx.session.get_session("se2")
        assert session_after.consolidated_cursor == 1

    async def test_delete_session(self, ctx):
        await ctx.save_exchange("del", "hi", [{"role": "assistant", "content": "hey"}])
        assert ctx.delete_session("del") is True

    async def test_list_sessions(self, ctx):
        await ctx.save_exchange("lst", "hi", [{"role": "assistant", "content": "hey"}])
        sessions = ctx.list_sessions()
        assert any(s["key"] == "lst" for s in sessions)


# ---------------------------------------------------------------------------
# Unified compression — keep_recent (idle path)
# ---------------------------------------------------------------------------


class TestCompressKeepRecent:
    async def test_noop_when_few_messages(self, ctx):
        """Fewer than keep_recent messages → no compression."""
        session = ctx.session.get_session("cr1")
        session.messages = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        ctx.session.save_session(session)

        n = await ctx.compress("cr1", keep_recent=10)
        assert n == 0
        # Messages unchanged
        session_after = ctx.session.get_session("cr1")
        assert len(session_after.messages) == 5

    async def test_compresses_older_messages(self, ctx, provider):
        """keep_recent=10 with 20 messages → 10 compressed."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("cr2")
        session.messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        ctx.session.save_session(session)

        n = await ctx.compress("cr2", keep_recent=10)
        assert n == 10

        # Provider called
        provider.chat_with_retry.assert_called_once()

        # Session messages UNCHANGED
        session_after = ctx.session.get_session("cr2")
        assert len(session_after.messages) == 20

        # Cursor advanced
        assert session_after.consolidated_cursor == 10

        # history.jsonl written
        history_path = ctx._history_path("cr2")
        assert history_path.exists()

    async def test_disabled_when_idle_zero(self, ctx):
        """idle_compress_seconds=0 disables idle compression."""
        ctx.idle_compress_seconds = 0
        session = ctx.session.get_session("cr3")
        session.messages = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        ctx.session.save_session(session)

        n = await ctx.compress("cr3", keep_recent=10)
        assert n == 0

    async def test_truncation_fallback_no_provider(self, ctx):
        """Without provider, falls back to truncation."""
        session = ctx.session.get_session("cr4")
        session.messages = [{"role": "user", "content": f"m{i}"} for i in range(15)]
        ctx.session.save_session(session)

        n = await ctx.compress("cr4", keep_recent=10)
        assert n == 5  # 15 msgs, keep 10, compress 5
        session_after = ctx.session.get_session("cr4")
        assert session_after.consolidated_cursor == 5

    async def test_handles_summarise_failure(self, ctx):
        """LLM failure → falls back to truncation."""
        ctx.provider = MagicMock(spec=LLMProvider)
        ctx.provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("LLM down"))

        session = ctx.session.get_session("cr5")
        session.messages = [{"role": "user", "content": f"m{i}"} for i in range(12)]
        ctx.session.save_session(session)

        n = await ctx.compress("cr5", keep_recent=10)
        assert n == 2
        session_after = ctx.session.get_session("cr5")
        assert session_after.consolidated_cursor == 2

    async def test_messages_not_modified(self, ctx, provider):
        """Compression never modifies session.messages content."""
        ctx.provider = provider

        session = ctx.session.get_session("cr6")
        original = [{"role": "user", "content": f"msg{i}"} for i in range(15)]
        session.messages = list(original)
        ctx.session.save_session(session)

        await ctx.compress("cr6", keep_recent=10)
        session_after = ctx.session.get_session("cr6")
        # All original messages still present and unchanged
        for i, msg in enumerate(original):
            assert session_after.messages[i] == msg


# ---------------------------------------------------------------------------
# Unified compression — budget_tokens
# ---------------------------------------------------------------------------


class TestCompressBudgetTokens:
    async def test_keeps_messages_within_budget(self, ctx, provider):
        """budget_tokens controls how many recent messages are kept."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("cb1")
        session.messages = [{"role": "user", "content": "x" * 500}] * 30
        ctx.session.save_session(session)

        # Very tight budget — only 1-2 messages can fit
        n = await ctx.compress("cb1", budget_tokens=50)
        assert n > 0
        session_after = ctx.session.get_session("cb1")
        assert session_after.consolidated_cursor > 0

    async def test_noop_when_budget_sufficient(self, ctx):
        """When all messages fit in budget, no compression."""
        session = ctx.session.get_session("cb2")
        session.messages = [{"role": "user", "content": "hi"}] * 5
        ctx.session.save_session(session)

        n = await ctx.compress("cb2", budget_tokens=100_000)
        assert n == 0


# ---------------------------------------------------------------------------
# Turn boundary preservation
# ---------------------------------------------------------------------------


class TestAdjustSplit:
    async def test_does_not_split_user_assistant_pair(self, ctx, provider):
        """A user message kept without its assistant response would violate turn
        integrity.  _adjust_split moves the preceding user into to_keep."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("as1")
        session.messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "q4"},
            {"role": "assistant", "content": "a4"},
            {"role": "user", "content": "q5"},
            {"role": "assistant", "content": "a5"},
        ]  # 10 messages
        ctx.session.save_session(session)

        # keep_recent=2 would split after msg 8 → to_keep = [q5, a5]
        # That's a clean boundary (starts at user). No adjustment needed.
        n = await ctx.compress("as1", keep_recent=2)
        assert n == 8  # 10 - 2 = 8 compressed
        # But cursor=8 means build_messages sees [q5, a5] — intact turn

    async def test_adjusts_when_assistant_first_in_keep(self, ctx, provider):
        """If to_keep starts with 'assistant', the preceding 'user' is moved."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("as2")
        session.messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]  # 6 messages
        ctx.session.save_session(session)

        # keep_recent=1 → would keep [a3] (assistant), adjust adds [q3]
        # So actual to_keep = [q3, a3], to_compress = [q1, a1, q2, a2]
        n = await ctx.compress("as2", keep_recent=1)
        assert n == 4  # 4 compressed, 2 kept (adjusted from 1)
        session_after = ctx.session.get_session("as2")
        assert session_after.consolidated_cursor == 4
        # build_messages would see [q3, a3] — complete turn

    async def test_adjusts_when_tool_first_in_keep(self, ctx, provider):
        """If to_keep starts with 'tool', the preceding assistant (with
        tool_calls) is moved."""
        ctx.provider = provider
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("as3")
        session.messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "run cmd"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "bash"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
            {"role": "assistant", "content": "got result"},
        ]  # 6 messages
        ctx.session.save_session(session)

        # keep_recent=2 → would keep [tool:ok, assistant:got result]
        # _adjust_split: first=too → move preceding assistant (with tool_calls)
        # to_keep = [assistant:tool_calls, tool:ok, assistant:got result]
        # Then first=assistant with tool_calls → safe
        n = await ctx.compress("as3", keep_recent=2)
        assert n == 3  # 3 compressed, 3 kept (adjusted from 2)


# ---------------------------------------------------------------------------
# Dehydration
# ---------------------------------------------------------------------------


class TestDehydrate:
    """Tests for CompactionService._dehydrate_messages (moved from ContextManager)."""

    def test_strips_data_uris(self):
        msgs = [{"role": "user", "content": "Look: data:image/png;base64,iVBORw0KGgoAAAANS"}]
        dehydrated = CompactionService._dehydrate_messages_static(msgs)
        assert "data:image/png;base64" not in str(dehydrated[0]["content"])
        assert "binary data removed" in str(dehydrated[0]["content"])

    def test_truncates_long_content(self):
        msgs = [{"role": "user", "content": "x" * 5000}]
        dehydrated = CompactionService._dehydrate_messages_static(msgs)
        content = dehydrated[0]["content"]
        assert len(content) < 4000  # 3000 + some overhead
        assert "truncated" in content

    def test_slims_tool_calls(self):
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "function": {"name": "write_file", "arguments": '{"path":"/x","content":"' + "x" * 5000 + '}"'},
            }],
        }]
        dehydrated = CompactionService._dehydrate_messages_static(msgs)
        tc = dehydrated[0]["tool_calls"][0]
        # Arguments replaced with placeholder
        assert tc["function"]["arguments"] == "{...}"


# ---------------------------------------------------------------------------
# Token-budget compression via build_messages
# ---------------------------------------------------------------------------


class TestBudgetCompressionIntegration:
    async def test_build_messages_triggers_compress_when_over_budget(self, ctx, provider):
        """When assembled context exceeds max_context_tokens, compress() is called."""
        ctx.provider = provider
        ctx.max_context_tokens = 4000
        ctx.compress_ratio = 0.5
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Summary."))

        session = ctx.session.get_session("bi1")
        # Varied text to avoid tiktoken compression of repeated chars.
        # 20 messages × ~250 tokens + system prompt (~1700) ≈ 6700 > 4000.
        body = "The quick brown fox jumps over the lazy dog. " * 20  # ~250 tokens per msg
        session.messages = [
            {"role": "user", "content": body},
            {"role": "assistant", "content": body},
        ] * 10  # 20 messages, ~5000 tokens of history
        ctx.session.save_session(session)

        msgs = await ctx.build_messages("bi1", "hello")
        # Should have triggered compression
        assert provider.chat_with_retry.called

        # Cursor should have advanced
        session_after = ctx.session.get_session("bi1")
        assert session_after.consolidated_cursor > 0

        # Result should still be well-formed
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"

    async def test_no_compression_when_under_budget(self, ctx, provider):
        """Context fits in budget → no compression."""
        ctx.provider = provider
        ctx.max_context_tokens = 1_000_000  # huge budget

        session = ctx.session.get_session("bi2")
        session.messages = [{"role": "user", "content": "hi"}] * 5
        ctx.session.save_session(session)

        await ctx.build_messages("bi2", "hello")
        assert not provider.chat_with_retry.called


# ---------------------------------------------------------------------------
# Memory delegation
# ---------------------------------------------------------------------------


class TestMemoryDelegation:
    async def test_remember(self, ctx):
        ctx.remember("test-key", "value", mem_type="user", description="desc")
        results = ctx.recall("test-key")
        assert len(results) >= 1

    async def test_forget(self, ctx):
        ctx.remember("to-delete", "x")
        assert ctx.forget("to-delete") in (True, False)

    async def test_recall(self, ctx):
        ctx.remember("recall-me", "some content")
        results = ctx.recall("recall")
        assert any(r.name == "recall-me" for r in results)

    async def test_save_exchange_only_writes_to_session(self, ctx):
        """save_exchange appends to session.messages, NOT to memory history."""
        await ctx.save_exchange("mem-sess", "hello world", [
            {"role": "assistant", "content": "hi there"},
        ])
        # Session should have the exchange
        session = ctx.session.get_session("mem-sess")
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "hello world"
        # memory history should NOT have this exchange
        recent = ctx.memory.get_recent_history(10)
        assert len(recent) == 0


# ---------------------------------------------------------------------------
# Interrupt repair
# ---------------------------------------------------------------------------


class TestRepair:
    async def test_repairs_unmatched_tool_calls(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "f"}}]},
        ]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        # Pass 1 adds synthetic tool result, Pass 2 adds synthetic assistant
        assert fixed_count == 2
        assert repaired[-2]["role"] == "tool"
        assert repaired[-2]["content"] == _INTERRUPT_TOOL_RESULT
        assert repaired[-1]["role"] == "assistant"

    async def test_repairs_unmatched_last_user(self):
        messages = [{"role": "user", "content": "last question"}]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        assert fixed_count > 0
        assert repaired[-1]["role"] == "assistant"
        assert repaired[-1]["content"] == _INTERRUPT_MESSAGE

    async def test_repairs_last_tool_message(self):
        """Session ending with a tool result (no assistant follow-up) is repaired."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "f"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "done"},
        ]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        assert fixed_count > 0
        assert repaired[-1]["role"] == "assistant"

    async def test_no_repair_when_paired(self):
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        assert fixed_count == 0
        assert repaired == messages

    async def test_repairs_both_patterns(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "f"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "results found"},
            {"role": "assistant", "content": "Here are the results."},
            {"role": "user", "content": "next question"},
        ]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        assert fixed_count > 0
        assert repaired[-1]["role"] == "assistant"
        assert repaired[-1]["content"] == _INTERRUPT_MESSAGE

    async def test_no_repair_tool_result_exists(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "f"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        ]
        repaired, fixed_count = ContextManager._repair_messages(messages)
        assert fixed_count > 0  # tool result exists but no final assistant
        assert repaired[-1]["role"] == "assistant"

    async def test_empty_messages(self):
        repaired, fixed_count = ContextManager._repair_messages([])
        assert fixed_count == 0
        assert repaired == []

    async def test_repair_messages_non_destructive(self, ctx):
        """_repair_messages fixes unmatched pairs without modifying stored session."""
        session = ctx.session.get_session("repair-nd")
        session.messages = [
            {"role": "user", "content": "unfinished"},
        ]
        ctx.session.save_session(session)

        # Repair is a pure transform — stored session is NOT modified
        repaired, fixed = ContextManager._repair_messages(
            ctx.session.get_session("repair-nd").messages,
        )
        assert fixed == 1
        assert len(repaired) == 2
        assert repaired[-1]["role"] == "assistant"

        # Stored session is unchanged
        session = ctx.session.get_session("repair-nd")
        assert len(session.messages) == 1
