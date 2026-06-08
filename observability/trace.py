"""Lightweight span-based tracing with async context propagation.

Uses :mod:`contextvars` to propagate the current span across ``asyncio``
task boundaries.  Spans are emitted as structured log events on completion.

Usage::

    from observability.trace import tracer

    with tracer.trace("orchestrator.process", session_key="abc"):
        with tracer.span("llm.chat", model="gpt-4"):
            ...
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Span / SpanContext
# ---------------------------------------------------------------------------


@dataclass
class SpanContext:
    """Immutable trace-identity triple carried across async boundaries."""

    trace_id: str
    span_id: str
    parent_span_id: str | None


@dataclass
class Span:
    """A single named operation within a trace."""

    name: str
    context: SpanContext
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _parent: Span | None = field(default=None, repr=False)

    @property
    def latency_ms(self) -> float:
        if self.end_time is None:
            return (time.monotonic() - self.start_time) * 1000
        return (self.end_time - self.start_time) * 1000


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


class Tracer:
    """Lightweight tracer that propagates spans through :mod:`contextvars`.

    Spans form a tree: each call to :meth:`start_span` creates a child of
    whatever span is currently active on the context.  :meth:`start_trace`
    creates a new root span (new ``trace_id``) even when a span is already
    active.
    """

    def __init__(self) -> None:
        self._current_span: contextvars.ContextVar[Span | None] = (
            contextvars.ContextVar("_trace_current_span", default=None)
        )

    # -- span creation ---------------------------------------------------------

    def start_trace(self, name: str, **attributes: Any) -> Span:
        """Start a new **root** span (new ``trace_id``), ignoring any active span."""
        ctx = SpanContext(
            trace_id=uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=None,
        )
        span = Span(name=name, context=ctx, attributes=dict(attributes))
        self._current_span.set(span)
        logger.debug("Trace  {}  started  name={!r}", ctx.trace_id, name)
        return span

    def start_span(self, name: str, **attributes: Any) -> Span:
        """Start a child span inheriting the current trace context.

        If no span is active a new trace is created automatically (root span).
        """
        parent = self._current_span.get()
        if parent is None:
            return self.start_trace(name, **attributes)

        ctx = SpanContext(
            trace_id=parent.context.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.context.span_id,
        )
        span = Span(name=name, context=ctx, attributes=dict(attributes), _parent=parent)
        self._current_span.set(span)
        return span

    # -- span completion -------------------------------------------------------

    def end_span(self, span: Span, status: str = "ok") -> None:
        """Finalise *span* and emit it as a structured log event."""
        span.end_time = time.monotonic()
        span.status = status

        # Restore parent as the current span
        self._current_span.set(span._parent)

        # Emit structured log
        logger.bind(
            event_type="Span",
            trace_id=span.context.trace_id,
            span_id=span.context.span_id,
            parent_span_id=span.context.parent_span_id,
            span_name=span.name,
            latency_ms=round(span.latency_ms, 3),
            status=span.status,
            **span.attributes,
        ).info(
            f"Span {span.name!r} {span.status} ({span.latency_ms:.2f} ms)"
        )

    # -- context managers ------------------------------------------------------

    @contextmanager
    def trace(self, name: str, **attributes: Any) -> Generator[Span, None, None]:
        """``with tracer.trace(...) as span:`` — new root span."""
        span = self.start_trace(name, **attributes)
        try:
            yield span
            self.end_span(span, "ok")
        except Exception:
            self.end_span(span, "error")
            raise

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Generator[Span, None, None]:
        """``with tracer.span(...) as span:`` — child span."""
        span = self.start_span(name, **attributes)
        try:
            yield span
            self.end_span(span, "ok")
        except Exception:
            self.end_span(span, "error")
            raise

    # -- helpers ---------------------------------------------------------------

    def current_span(self) -> Span | None:
        """Return the currently-active span, if any."""
        return self._current_span.get()

    def current_trace_id(self) -> str | None:
        """Return the current ``trace_id``, or ``None``."""
        s = self._current_span.get()
        return s.context.trace_id if s else None

    def set_attribute(self, key: str, value: Any) -> None:
        """Set an attribute on the currently-active span."""
        s = self._current_span.get()
        if s is not None:
            s.attributes[key] = value

    def add_event(self, name: str, **attributes: Any) -> None:
        """Add a timestamped event to the current span."""
        s = self._current_span.get()
        if s is not None:
            s.events.append({"name": name, "timestamp": time.monotonic(), **attributes})


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


tracer = Tracer()
"""Global tracer instance shared across the process."""
