"""Lightweight async event bus for decoupled publish/subscribe.

Sits between producers (runner, orchestrator) and consumers (metrics,
logging, display, WebSocket, webhooks).  Producers publish events; any
number of async subscribers react to them.

Usage::

    from core.events import bus, ToolExecutionCompleted

    async def my_handler(event: ToolExecutionCompleted) -> None:
        print(f"{event.tool_name} done in {event.latency_ms:.0f}ms")

    bus.subscribe(ToolExecutionCompleted, my_handler)

    # In runner.py:
    await bus.publish(ToolExecutionCompleted(
        session_key="abc", tool_name="bash", success=True, latency_ms=234.5,
    ))
"""

from __future__ import annotations

import asyncio
import time as _time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

# Handler signature: async (event) -> None
EventHandler = Callable[[Any], Awaitable[None]]


@dataclass
class Event:
    """Base event with timestamp."""
    timestamp: float = field(default_factory=_time.monotonic)


# -- Agent lifecycle --------------------------------------------------------


@dataclass
class AgentStarted(Event):
    """Emitted when an agent run begins."""
    session_key: str = ""
    paradigm: str = ""
    messages_count: int = 0
    tools_count: int = 0


@dataclass
class AgentStepStarted(Event):
    """Emitted at the start of each agent loop iteration."""
    session_key: str = ""
    step_count: int = 0


@dataclass
class AgentStepCompleted(Event):
    """Emitted at the end of each agent loop iteration."""
    session_key: str = ""
    step_count: int = 0
    had_tool_calls: bool = False


@dataclass
class AgentCompleted(Event):
    """Emitted when an agent run finishes (success or exhaustion)."""
    session_key: str = ""
    paradigm: str = ""
    steps: int = 0
    total_latency_ms: float = 0.0
    stop_reason: str = "completed"
    error: str | None = None


@dataclass
class AgentStallWarning(Event):
    """Emitted when step count exceeds the stall threshold."""
    session_key: str = ""
    step_count: int = 0


# -- LLM calls --------------------------------------------------------------


@dataclass
class LLMResponseReady(Event):
    """Emitted after every LLM call (chat or stream)."""
    session_key: str = ""
    step_count: int = 0
    model: str = ""
    latency_ms: float = 0.0
    messages_count: int = 0
    tools_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    finish_reason: str = "stop"
    error: str | None = None


# -- Tool execution ---------------------------------------------------------


@dataclass
class ToolExecutionStarted(Event):
    """Emitted just before a tool executes."""
    session_key: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    index: int = 0
    total: int = 0


@dataclass
class ToolExecutionCompleted(Event):
    """Emitted after a tool finishes (success or failure)."""
    session_key: str = ""
    tool_name: str = ""
    success: bool = True
    latency_ms: float = 0.0
    error: str | None = None
    arguments_preview: str = ""
    detail: str = ""


# -- Session lifecycle ------------------------------------------------------


@dataclass
class SessionCreated(Event):
    """Emitted when a new session is created."""
    session_key: str = ""


@dataclass
class SessionDeleted(Event):
    """Emitted when a session is deleted."""
    session_key: str = ""


@dataclass
class SessionCompressed(Event):
    """Emitted after context compression."""
    session_key: str = ""
    compressed_count: int = 0


# -- Memory -----------------------------------------------------------------


@dataclass
class MemoryChanged(Event):
    """Emitted when a long-term memory entry is created, updated, or deleted."""
    action: str = ""  # "remember" | "forget"
    name: str = ""
    mem_type: str = ""


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Async publish/subscribe bus.

    Subscribers register for a specific event type.  When an event is
    published, all matching subscribers are invoked concurrently via
    ``asyncio.gather``.  Subscriber exceptions are logged but never
    propagate to the publisher.

    Supports inheritance-aware subscription: subscribing to a base class
    receives all subclass events.  E.g. subscribing to ``Event`` catches
    everything (useful for global audit logging).
    """

    def __init__(self) -> None:
        # event_type -> list of async handlers
        self._subscribers: dict[type, list[EventHandler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, event_type: type, handler: EventHandler) -> None:
        """Register *handler* to be called for events of type *event_type*.

        Handlers are called concurrently.  Subscribe to ``Event`` to catch
        all events (useful for audit / global logging).
        """
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: EventHandler) -> None:
        """Remove a previously registered handler."""
        try:
            self._subscribers[event_type].remove(handler)
        except ValueError:
            pass

    async def publish(self, event: Event) -> None:
        """Fan out *event* to all matching subscribers concurrently.

        Matching is done by ``isinstance`` check — subscribers to a base
        class receive subclass events.
        """
        if not self._subscribers:
            return

        # Collect matching handlers (iterate over a snapshot to allow
        # publish-from-within-subscriber without re-entrancy issues)
        matched: list[EventHandler] = []
        for ev_type, handlers in self._subscribers.items():
            if isinstance(event, ev_type):
                matched.extend(handlers)

        if not matched:
            return

        results = await asyncio.gather(
            *(self._invoke(h, event) for h in matched),
            return_exceptions=True,
        )
        for raw in results:
            if isinstance(raw, BaseException):
                logger.opt(exception=raw).warning(
                    "EventBus subscriber raised an exception"
                )

    @staticmethod
    async def _invoke(handler: EventHandler, event: Event) -> None:
        """Wrap handler call with exception isolation."""
        await handler(event)

    def clear(self) -> None:
        """Remove all subscribers (useful in tests)."""
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        """Total number of registered handler entries."""
        return sum(len(v) for v in self._subscribers.values())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


bus = EventBus()
"""Global event bus instance shared across the process."""
