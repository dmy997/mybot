"""Agent middleware — intercept and modify LLM calls, tool execution, and agent lifecycle.

Middleware wraps the core agent loop via a chain-of-responsibility pattern.
Each method receives a :class:`MiddlewareContext` and a ``call_next`` async
callable.  Call ``call_next(ctx)`` to proceed to the next middleware (or the
actual handler).  Modify *ctx* before calling, inspect the result after, or
skip ``call_next`` entirely to short-circuit.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from providers.base import LLMResponse
from tools.registry import ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class MiddlewareContext:
    """Mutable context threaded through every middleware call.

    Fields are populated depending on the hook point:

    - ``on_agent_start`` / ``on_agent_step`` / ``on_agent_end``:
      *messages*, *session_key*, *step_count*
    - ``on_llm_call``: additionally *model*, *temperature*, *max_tokens*,
      *tool_defs*.  *llm_response* is set by ``call_next``.
    - ``on_tool_execute``: *tool_name*, *tool_arguments*.  *tool_result* is
      set by ``call_next``.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    session_key: str = ""
    step_count: int = 0

    # LLM-call fields
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tool_defs: list[dict[str, Any]] | None = None
    llm_response: LLMResponse | None = None

    # Tool-execution fields
    tool_name: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: ToolResult | None = None
    tools: ToolRegistry | None = None

    # Arbitrary payload shared across middleware
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Callable types
# ---------------------------------------------------------------------------

LlmNext = Callable[[MiddlewareContext], Awaitable[LLMResponse]]
ToolNext = Callable[[MiddlewareContext], Awaitable[ToolResult]]
StepNext = Callable[[MiddlewareContext], Awaitable[bool]]

# ---------------------------------------------------------------------------
# Middleware base
# ---------------------------------------------------------------------------


class AgentMiddleware(ABC):
    """Base middleware with no-op defaults for every hook point.

    Subclass and override only the methods you need.
    """

    async def on_agent_start(self, ctx: MiddlewareContext) -> None:
        """Called once before the agent loop begins."""

    async def on_agent_step(
        self, ctx: MiddlewareContext, call_next: StepNext,
    ) -> bool:
        """Called at each loop iteration.  Return ``False`` to abort the loop."""
        return await call_next(ctx)

    async def on_llm_call(
        self, ctx: MiddlewareContext, call_next: LlmNext,
    ) -> LLMResponse:
        """Wrap an LLM API call.

        Modify *ctx.messages*, *ctx.model*, etc. before ``call_next``.
        Inspect or replace *ctx.llm_response* after.
        """
        return await call_next(ctx)

    async def on_tool_execute(
        self, ctx: MiddlewareContext, call_next: ToolNext,
    ) -> ToolResult:
        """Wrap a tool execution.

        Inspect *ctx.tool_name* / *ctx.tool_arguments* before ``call_next``.
        Inspect or replace *ctx.tool_result* after.  Return a synthetic
        :class:`ToolResult` without calling ``call_next`` to block execution.
        """
        return await call_next(ctx)

    async def on_agent_end(
        self, ctx: MiddlewareContext, output: AgentOutput | None = None,
    ) -> None:
        """Called once after the agent loop finishes (or errors)."""


# Needed for type annotation below
from core.runner import AgentOutput  # noqa: E402

# ---------------------------------------------------------------------------
# Chain runner
# ---------------------------------------------------------------------------


class MiddlewareChain:
    """Ordered list of middleware, invoked in registration order.

    Each ``run_*`` method builds a nested ``call_next`` chain so that
    middleware[n] wraps middleware[n+1], with the actual handler at the
    bottom.
    """

    def __init__(self, middlewares: list[AgentMiddleware] | None = None) -> None:
        self._middlewares: list[AgentMiddleware] = list(middlewares or [])

    def add(self, mw: AgentMiddleware) -> None:
        """Append a middleware to the end of the chain."""
        self._middlewares.append(mw)

    def __bool__(self) -> bool:
        return bool(self._middlewares)

    # -- agent start / end (simple sequential) ------------------------------

    async def run_agent_start(self, ctx: MiddlewareContext) -> None:
        for mw in self._middlewares:
            await mw.on_agent_start(ctx)

    async def run_agent_end(
        self, ctx: MiddlewareContext, output: AgentOutput | None = None,
    ) -> None:
        for mw in self._middlewares:
            await mw.on_agent_end(ctx, output)

    # -- agent step (chain — earlier middleware can abort) -------------------

    async def run_agent_step(
        self, ctx: MiddlewareContext, handler: StepNext,
    ) -> bool:
        return await self._build_chain(
            [mw.on_agent_step for mw in self._middlewares], handler, ctx
        )

    # -- LLM call (chain — modify args before, inspect response after) -----

    async def run_llm_call(
        self, ctx: MiddlewareContext, handler: LlmNext,
    ) -> LLMResponse:
        return await self._build_chain(
            [mw.on_llm_call for mw in self._middlewares], handler, ctx
        )

    # -- tool execute (chain — block, modify, or cache results) ------------

    async def run_tool_execute(
        self, ctx: MiddlewareContext, handler: ToolNext,
    ) -> ToolResult:
        return await self._build_chain(
            [mw.on_tool_execute for mw in self._middlewares], handler, ctx
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_chain(
        mw_methods: list[Callable[..., Any]],
        handler: Any,
        ctx: MiddlewareContext,
    ) -> Any:
        """Recursively build a nested call chain.

        ``mw[0](ctx, lambda ctx: mw[1](ctx, lambda ctx: ... handler(ctx)))``
        """
        idx = 0

        async def _dispatch(*, _i: int = 0) -> Any:
            nonlocal idx
            if _i >= len(mw_methods):
                return await handler(ctx)
            return await mw_methods[_i](ctx, lambda ctx: _dispatch(_i=_i + 1))

        return _dispatch(_i=0)
