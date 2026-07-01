"""In-memory ring buffers for recent observability events.

Exposes two global stores that are populated by hooks in :mod:`observability.log`
and :mod:`observability.trace`, then served via HTTP endpoints.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

MAX_LOGS = 500
MAX_SPANS = 200


@dataclass
class LogEntry:
    timestamp: float
    event_type: str
    data: dict[str, Any]


@dataclass
class SpanEntry:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time: float
    end_time: float | None
    latency_ms: float
    status: str
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class RecentStore:
    """Thread-safe ring buffers for recent log entries and completed spans."""

    def __init__(self) -> None:
        self._logs: deque[LogEntry] = deque(maxlen=MAX_LOGS)
        self._spans: deque[SpanEntry] = deque(maxlen=MAX_SPANS)

    # -- logs -----------------------------------------------------------------

    def add_log(self, event_type: str, data: dict[str, Any]) -> None:
        self._logs.append(LogEntry(
            timestamp=time.time(),
            event_type=event_type,
            data=data,
        ))

    def get_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        entries = list(self._logs)[-limit:]
        return [
            {"timestamp": e.timestamp, "event_type": e.event_type, "data": e.data}
            for e in entries
        ]

    # -- spans -----------------------------------------------------------------

    def add_span(self, span: Any) -> None:
        """Store a completed :class:`Span`."""
        self._spans.append(SpanEntry(
            trace_id=span.context.trace_id,
            span_id=span.context.span_id,
            parent_span_id=span.context.parent_span_id,
            name=span.name,
            start_time=span.start_time,
            end_time=span.end_time,
            latency_ms=round(span.latency_ms, 3),
            status=span.status,
            attributes=dict(span.attributes),
            events=list(span.events),
            input=dict(span.input) if span.input else None,
            output=dict(span.output) if span.output else None,
        ))

    def get_spans(self, limit: int = 100) -> list[dict[str, Any]]:
        entries = list(self._spans)[-limit:]
        return [
            {
                "trace_id": e.trace_id,
                "span_id": e.span_id,
                "parent_span_id": e.parent_span_id,
                "name": e.name,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "latency_ms": e.latency_ms,
                "status": e.status,
                "attributes": e.attributes,
                "events": e.events,
                "input": e.input,
                "output": e.output,
            }
            for e in entries
        ]

    def clear(self) -> None:
        self._logs.clear()
        self._spans.clear()


# Module-level singleton
recent = RecentStore()
