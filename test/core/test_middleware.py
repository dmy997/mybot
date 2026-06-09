"""Tests for core/middleware.py — middleware chain and hooks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.middleware import AgentMiddleware, MiddlewareChain, MiddlewareContext
from core.runner import AgentCore, AgentInput
from providers.base import LLMProvider, LLMResponse
from tools.registry import ToolRegistry, ToolResult
from tools.tool import Tool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    """Mock LLM provider that returns a simple response."""
    p = MagicMock(spec=LLMProvider)
    p.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="hello", finish_reason="stop")
    )
    return p


@pytest.fixture
def core(provider):
    """AgentCore without middleware."""
    return AgentCore(provider)


@pytest.fixture
def tools():
    """Tool registry with a simple echo tool."""
    t = ToolRegistry()

    class _Echo(Tool):
        name = "echo"
        description = "Echo back input"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
        parallel = True
        capabilities = set()

        async def execute(self, text: str = "") -> ToolResult:
            return ToolResult(success=True, content=text)

    t.register(_Echo())
    return t


# ---------------------------------------------------------------------------
# MiddlewareContext
# ---------------------------------------------------------------------------


class TestMiddlewareContext:
    def test_defaults(self):
        ctx = MiddlewareContext()
        assert ctx.messages == []
        assert ctx.session_key == ""
        assert ctx.step_count == 0
        assert ctx.model is None
        assert ctx.llm_response is None
        assert ctx.tool_result is None
        assert ctx.data == {}

    def test_data_dict_is_independent(self):
        ctx1 = MiddlewareContext()
        ctx2 = MiddlewareContext()
        ctx1.data["x"] = 1
        assert "x" not in ctx2.data


# ---------------------------------------------------------------------------
# MiddlewareChain
# ---------------------------------------------------------------------------


class TestMiddlewareChain:

    def test_empty_chain_is_falsy(self):
        chain = MiddlewareChain()
        assert not chain

    def test_nonempty_chain_is_truthy(self):
        chain = MiddlewareChain([_NoopMiddleware()])
        assert chain

    def test_add_middleware(self):
        chain = MiddlewareChain()
        chain.add(_NoopMiddleware())
        assert chain

    # -- agent_start / agent_end -----------------------------------------

    @pytest.mark.asyncio
    async def test_run_agent_start_calls_each(self):
        calls = []
        chain = MiddlewareChain([_LoggingMiddleware(calls)])
        ctx = MiddlewareContext()
        await chain.run_agent_start(ctx)
        assert calls == ["start"]

    @pytest.mark.asyncio
    async def test_run_agent_end_calls_each(self):
        calls = []
        chain = MiddlewareChain([_LoggingMiddleware(calls)])
        ctx = MiddlewareContext()
        await chain.run_agent_end(ctx, None)
        assert calls == ["end"]

    # -- agent_step ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_agent_step_continue(self):
        """Middleware returning True allows the loop to continue."""

        async def handler(c):
            return True

        chain = MiddlewareChain([_NoopMiddleware()])
        ctx = MiddlewareContext()
        result = await chain.run_agent_step(ctx, handler)
        assert result is True

    @pytest.mark.asyncio
    async def test_agent_step_abort(self):
        """Middleware can abort the loop by returning False."""

        class _Abort(AgentMiddleware):
            async def on_agent_step(self, ctx, call_next):
                # Don't call call_next — abort immediately
                return False

        async def handler(c):
            return True

        chain = MiddlewareChain([_Abort()])
        ctx = MiddlewareContext()
        result = await chain.run_agent_step(ctx, handler)
        assert result is False

    # -- llm_call --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_llm_call_wraps_handler(self):
        """Middleware sees the LLM response."""

        seen: list[LLMResponse | None] = []

        class _Spy(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                resp = await call_next(ctx)
                seen.append(resp)
                return resp

        async def handler(c):
            resp = LLMResponse(content="model output", finish_reason="stop")
            c.llm_response = resp
            return resp

        chain = MiddlewareChain([_Spy()])
        ctx = MiddlewareContext()
        result = await chain.run_llm_call(ctx, handler)
        assert result.content == "model output"
        assert len(seen) == 1
        assert seen[0].content == "model output"

    @pytest.mark.asyncio
    async def test_llm_call_modify_messages(self):
        """Middleware can modify messages before the LLM call."""

        class _Inject(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                ctx.messages.append({"role": "system", "content": "injected"})
                return await call_next(ctx)

        captured_messages: list[list] = []

        async def handler(c):
            captured_messages.append(list(c.messages))
            resp = LLMResponse(content="ok", finish_reason="stop")
            c.llm_response = resp
            return resp

        chain = MiddlewareChain([_Inject()])
        ctx = MiddlewareContext(messages=[{"role": "user", "content": "hi"}])
        await chain.run_llm_call(ctx, handler)
        assert len(captured_messages[0]) == 2
        assert captured_messages[0][1]["content"] == "injected"

    @pytest.mark.asyncio
    async def test_llm_call_short_circuit(self):
        """Middleware can skip the actual LLM call entirely."""

        class _Cache(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                # Don't call call_next — return cached response
                return LLMResponse(content="cached", finish_reason="stop")

        handler_called = False

        async def handler(c):
            nonlocal handler_called
            handler_called = True
            return LLMResponse(content="real", finish_reason="stop")

        chain = MiddlewareChain([_Cache()])
        ctx = MiddlewareContext()
        result = await chain.run_llm_call(ctx, handler)
        assert result.content == "cached"
        assert not handler_called

    # -- tool_execute ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_tool_execute_wraps_handler(self):
        """Middleware can wrap tool execution."""

        events: list[str] = []

        class _Audit(AgentMiddleware):
            async def on_tool_execute(self, ctx, call_next):
                events.append(f"before:{ctx.tool_name}")
                result = await call_next(ctx)
                events.append(f"after:{ctx.tool_name}")
                return result

        async def handler(c):
            return ToolResult(success=True, content="done")

        chain = MiddlewareChain([_Audit()])
        ctx = MiddlewareContext(tool_name="echo", tool_arguments={"x": 1})
        result = await chain.run_tool_execute(ctx, handler)
        assert result.success
        assert events == ["before:echo", "after:echo"]

    @pytest.mark.asyncio
    async def test_tool_execute_block(self):
        """Middleware can block tool execution."""

        class _Guard(AgentMiddleware):
            async def on_tool_execute(self, ctx, call_next):
                if ctx.tool_name == "rm":
                    return ToolResult(success=False, content="", error="Blocked")
                return await call_next(ctx)

        async def handler(c):
            return ToolResult(success=True, content="executed")

        chain = MiddlewareChain([_Guard()])

        # Blocked tool
        ctx = MiddlewareContext(tool_name="rm")
        result = await chain.run_tool_execute(ctx, handler)
        assert not result.success
        assert "Blocked" in (result.error or "")

        # Allowed tool
        ctx2 = MiddlewareContext(tool_name="echo")
        result2 = await chain.run_tool_execute(ctx2, handler)
        assert result2.success

    # -- ordering --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_middleware_order(self):
        """Middleware runs in registration order — first registered wraps outermost."""

        log: list[str] = []

        class _A(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                log.append("A-before")
                resp = await call_next(ctx)
                log.append("A-after")
                return resp

        class _B(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                log.append("B-before")
                resp = await call_next(ctx)
                log.append("B-after")
                return resp

        async def handler(c):
            log.append("handler")
            return LLMResponse(content="ok", finish_reason="stop")

        chain = MiddlewareChain([_A(), _B()])
        await chain.run_llm_call(MiddlewareContext(), handler)
        # A registered first → outermost, so: A-before, B-before, handler, B-after, A-after
        assert log == ["A-before", "B-before", "handler", "B-after", "A-after"]


# ---------------------------------------------------------------------------
# Integration: AgentCore with middleware
# ---------------------------------------------------------------------------


class TestAgentCoreIntegration:
    """Verify middleware is called during real AgentCore.run()."""

    @pytest.mark.asyncio
    async def test_on_llm_call_invoked(self, provider, tools):
        events: list[str] = []

        class _Spy(AgentMiddleware):
            async def on_llm_call(self, ctx, call_next):
                events.append(f"model:{ctx.model}")
                return await call_next(ctx)

        chain = MiddlewareChain([_Spy()])
        core = AgentCore(provider, middleware=chain)
        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            model="gpt-4o",
        ))
        assert result.content == "hello"
        assert "model:gpt-4o" in events

    @pytest.mark.asyncio
    async def test_on_tool_execute_invoked(self, provider, tools):
        """Middleware wraps tool execution during agent run."""
        provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(
                content="",
                tool_calls=[_make_tc("echo", {"text": "ping"}, "c1")],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ])

        events: list[str] = []

        class _Spy(AgentMiddleware):
            async def on_tool_execute(self, ctx, call_next):
                events.append(f"tool:{ctx.tool_name}")
                return await call_next(ctx)

        chain = MiddlewareChain([_Spy()])
        core = AgentCore(provider, middleware=chain)
        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "echo something"}],
            tools=tools,
        ))
        assert "tool:echo" in events
        assert result.content == "done"

    @pytest.mark.asyncio
    async def test_on_agent_start_end_invoked(self, provider, tools):
        events: list[str] = []

        class _Lifecycle(AgentMiddleware):
            async def on_agent_start(self, ctx):
                events.append("start")

            async def on_agent_end(self, ctx, output):
                events.append(f"end:{output.stop_reason if output else 'none'}")

        chain = MiddlewareChain([_Lifecycle()])
        core = AgentCore(provider, middleware=chain)
        await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        ))
        assert events == ["start", "end:stop"]

    @pytest.mark.asyncio
    async def test_on_agent_step_abort(self, provider, tools):
        """on_agent_step can abort the loop."""

        class _MaxSteps(AgentMiddleware):
            async def on_agent_step(self, ctx, call_next):
                if ctx.step_count >= 2:
                    return False
                return await call_next(ctx)

        # Provider returns tool_calls to force multiple steps
        provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(
                content="",
                tool_calls=[_make_tc("echo", {"text": "a"}, "c1")],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[_make_tc("echo", {"text": "b"}, "c2")],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="never reached", finish_reason="stop"),
        ])

        chain = MiddlewareChain([_MaxSteps()])
        core = AgentCore(provider, middleware=chain)
        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "loop"}],
            tools=tools,
        ))
        assert result.stop_reason == "middleware"
        assert "stopped by middleware" in result.content.lower()

    @pytest.mark.asyncio
    async def test_agent_end_called_on_error(self, provider, tools):
        """on_agent_end fires even when LLM returns an error."""
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="fail", finish_reason="error")
        )

        ended: list[str] = []

        class _Spy(AgentMiddleware):
            async def on_agent_end(self, ctx, output):
                ended.append(output.stop_reason if output else "none")

        chain = MiddlewareChain([_Spy()])
        core = AgentCore(provider, middleware=chain)
        await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        ))
        assert "error" in ended

    @pytest.mark.asyncio
    async def test_no_middleware_still_works(self, core, tools):
        """AgentCore without middleware works exactly as before."""
        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        ))
        assert result.content == "hello"
        assert result.stop_reason == "stop"


# ---------------------------------------------------------------------------
# End-to-end: Orchestrator with middleware
# ---------------------------------------------------------------------------


class TestOrchestratorMiddleware:
    """Verify middleware flows correctly through orchestrator → agents."""

    @pytest.mark.asyncio
    async def test_process_message_with_middleware(self, provider, tools):
        """Middleware chain flows correctly through AgentCore."""
        events: list[str] = []

        class _Spy(AgentMiddleware):
            async def on_agent_start(self, ctx):
                events.append("mw_start")

            async def on_agent_end(self, ctx, output):
                events.append("mw_end")

        chain = MiddlewareChain([_Spy()])

        # We can't easily construct a full Orchestrator without config,
        # so verify the chain flows correctly via AgentCore directly
        core = AgentCore(provider, middleware=chain)
        result = await core.run(AgentInput(
            init_messages=[{"role": "user", "content": "test"}],
            tools=tools,
        ))
        assert "mw_start" in events
        assert "mw_end" in events
        assert result.content == "hello"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tc(name: str, args: dict, tc_id: str) -> Any:
    """Build a mock ToolCallRequest."""
    from providers.base import ToolCallRequest
    return ToolCallRequest(id=tc_id, name=name, arguments=args)


class _NoopMiddleware(AgentMiddleware):
    """Middleware that does nothing — all defaults."""


class _LoggingMiddleware(AgentMiddleware):
    """Records lifecycle events."""

    def __init__(self, log: list[str]):
        self.log = log

    async def on_agent_start(self, ctx):
        self.log.append("start")

    async def on_agent_end(self, ctx, output):
        self.log.append("end")
