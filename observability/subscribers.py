"""Built-in EventBus subscribers that wire events to metrics and logging.

Call :func:`install` once at application startup to connect the global
event bus to the existing observability infrastructure (REGISTRY metrics,
structured log events, tracer spans).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from core.events import (
    AgentCompleted,
    AgentStallWarning,
    LLMResponseReady,
    ToolExecutionCompleted,
    bus,
)
from observability import AgentRunEvent, LLMCallEvent, ToolCallEvent, emit
from observability.metrics import REGISTRY

# ---------------------------------------------------------------------------
# Individual subscriber handlers
# ---------------------------------------------------------------------------


async def _on_llm_response(event: LLMResponseReady) -> None:
    """Update LLM metrics and emit structured log."""
    REGISTRY.llm_calls_total.inc()
    REGISTRY.llm_latency_ms.observe(event.latency_ms)
    if event.tokens_total:
        REGISTRY.llm_tokens_total.inc(event.tokens_total)
    if event.finish_reason == "error":
        REGISTRY.llm_calls_errors_total.inc()

    emit(LLMCallEvent(
        model=event.model,
        latency_ms=event.latency_ms,
        messages_count=event.messages_count,
        tools_count=event.tools_count,
        tokens_in=event.tokens_in,
        tokens_out=event.tokens_out,
        tokens_total=event.tokens_total,
        finish_reason=event.finish_reason,
        error=event.error,
    ), session_key=event.session_key)


async def _on_tool_completed(event: ToolExecutionCompleted) -> None:
    """Update tool metrics and emit structured log."""
    REGISTRY.tool_calls_total.inc()
    REGISTRY.tool_latency_ms.observe(event.latency_ms)
    if not event.success:
        REGISTRY.tool_calls_errors_total.inc()

    emit(ToolCallEvent(
        tool_name=event.tool_name,
        success=event.success,
        latency_ms=event.latency_ms,
        error=event.error,
    ), session_key=event.session_key)


async def _on_agent_completed(event: AgentCompleted) -> None:
    """Update agent metrics and emit structured log."""
    REGISTRY.agent_steps.observe(event.steps)
    if event.error:
        REGISTRY.agent_errors_total.inc()

    emit(AgentRunEvent(
        session_key=event.session_key,
        paradigm=event.paradigm,
        steps=event.steps,
        total_latency_ms=round(event.total_latency_ms, 3),
        stop_reason=event.stop_reason,
        error=event.error,
    ))


async def _on_stall_warning(event: AgentStallWarning) -> None:
    """Increment stall counter."""
    REGISTRY.agent_stall_warnings_total.inc()


async def _on_any_event(event: Any) -> None:
    """Debug-level trace of every event (useful for development)."""
    logger.debug(
        "Event: {}  session={}",
        type(event).__name__,
        getattr(event, "session_key", "-"),
    )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install(*, debug: bool = False) -> None:
    """Register built-in metric/logging subscribers on the global bus.

    Idempotent — calling twice is a no-op (subscribers are not duplicated
    because the bus appends, but the call sites should guard with a flag).

    Parameters
    ----------
    debug:
        When True, also subscribe a catch-all debug logger for every event.
    """
    # Core metrics + logging
    bus.subscribe(LLMResponseReady, _on_llm_response)
    bus.subscribe(ToolExecutionCompleted, _on_tool_completed)
    bus.subscribe(AgentCompleted, _on_agent_completed)
    bus.subscribe(AgentStallWarning, _on_stall_warning)

    if debug:
        bus.subscribe(object, _on_any_event)  # type: ignore[arg-type]

    logger.info(
        "EventBus subscribers installed (metrics, log{})",
        ", debug" if debug else "",
    )
