"""Observability: structured logging, tracing, metrics, and display."""

from .display import (
    console,
    print_error,
    print_plain,
    render_content,
    show_banner,
    show_history,
    show_sessions,
    show_tool_results,
)
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
from .otel_bridge import OTelBridge, auto_install, otel_available
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
    # otel bridge
    "OTelBridge",
    "auto_install",
    "otel_available",
    # display
    "show_banner",
    "show_tool_results",
    "show_history",
    "show_sessions",
    "render_content",
    "print_plain",
    "print_error",
    "console",
]
