"""Observability: structured logging, tracing, and metrics."""

from .log import (
    AgentRunEvent,
    LLMCallEvent,
    LogConfig,
    SessionEvent,
    ToolCallEvent,
    emit,
    init_logging,
)
from .metrics import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    MetricsRegistrySnapshot,
)
from .trace import Span, SpanContext, Tracer, tracer

__all__ = [
    # log
    "LogConfig",
    "init_logging",
    "emit",
    "LLMCallEvent",
    "ToolCallEvent",
    "SessionEvent",
    "AgentRunEvent",
    # trace
    "Tracer",
    "Span",
    "SpanContext",
    "tracer",
    # metrics
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "MetricsRegistrySnapshot",
    "REGISTRY",
]
