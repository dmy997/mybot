"""Structured logging configuration and typed event schemas.

Usage::

    from observability import init_logging, LogConfig, emit, LLMCallEvent
    init_logging(LogConfig(level="DEBUG", log_dir=Path("./logs")))

    emit(LLMCallEvent(model="gpt-4", latency_ms=1234.5, ...))
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LogConfig:
    """Logging configuration for the observability layer."""

    level: str = "WARNING"
    """Minimum log level for console output.  Default is WARNING to keep the
    terminal clean during interactive use.  Use ``"DEBUG"`` for development."""

    file_level: str = "DEBUG"
    """Minimum log level for file output.  File logs always capture everything
    regardless of the console level."""

    json_format: bool = False
    """When True, file logs use JSON serialization (always JSON for file)."""

    log_dir: Path | None = None
    """Directory for rotating file logs.  If None, only console logging."""

    rotation: str = "10 MB"
    """Rotate file when it exceeds this size."""

    retention: str = "7 days"
    """Keep rotated logs for this duration."""

    # -- internal bookkeeping --
    _initialized: bool = field(default=False, repr=False)


def init_logging(config: LogConfig | None = None) -> None:
    """Configure loguru handlers once at application startup.

    Idempotent — calling it again is a no-op unless *config* has not
    been applied (tracked via ``config._initialized``).

    Adds:
    - A coloured stderr handler for human-readable output.
    - A rotating JSON file handler when ``config.log_dir`` is set.
    """
    if config is None:
        config = LogConfig()

    if config._initialized:
        return

    logger.remove()  # drop the default stderr handler

    # Ensure every log record has an "event_type" in extra so format strings
    # that reference {extra[event_type]} never fail with KeyError.
    def _patch_defaults(record: dict) -> None:
        record["extra"].setdefault("event_type", "")

    logger.configure(patcher=_patch_defaults)

    # -- console (coloured, non-JSON) ------------------------------------------
    logger.add(
        sys.stderr,
        level=config.level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[event_type]:<20}</cyan> | "
            "<level>{message}</level>"
        ),
        filter=lambda record: record["level"].no >= logger.level(config.level).no,
    )

    # -- file (JSON, rotating, always DEBUG) -----------------------------------
    if config.log_dir is not None:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.log_dir / "mybot_{time:YYYY-MM-DD}.log",
            level=config.file_level,
            rotation=config.rotation,
            retention=config.retention,
            serialize=True,  # JSON lines
            format="{extra}",
        )

    config._initialized = True
    logger.info("Observability logging configured (console={}, file={}, dir={})",
                config.level, config.file_level, config.log_dir or "none")


# ---------------------------------------------------------------------------
# Structured event types
# ---------------------------------------------------------------------------


@dataclass
class LLMCallEvent:
    """Emitted after every LLM call (chat or stream)."""

    model: str
    latency_ms: float
    messages_count: int
    tools_count: int
    tokens_in: int
    tokens_out: int
    tokens_total: int
    finish_reason: str
    error: str | None = None


@dataclass
class ToolCallEvent:
    """Emitted after each tool execution."""

    tool_name: str
    success: bool
    latency_ms: float
    error: str | None = None


@dataclass
class SessionEvent:
    """Emitted on session lifecycle transitions."""

    session_key: str
    action: str  # "created" | "resumed" | "deleted" | "compressed"
    message_count: int = 0


@dataclass
class AgentRunEvent:
    """Emitted when an agent run completes (success or failure)."""

    session_key: str
    paradigm: str
    steps: int
    total_latency_ms: float
    stop_reason: str
    error: str | None = None


@dataclass
class MemorySearchEvent:
    """Emitted when memory recall is performed."""

    query: str
    mode: str  # "hybrid" | "fts5_only" | "substring"
    result_count: int
    latency_ms: float = 0.0
    session_key: str | None = None


# ---------------------------------------------------------------------------
# emit helper
# ---------------------------------------------------------------------------


def emit(event: Any, *, level: str = "INFO", session_key: str | None = None) -> None:
    """Emit a structured event through loguru.

    Event fields are bound to the log record's ``extra`` dict so they
    appear as top-level keys in JSON output.
    """
    data = _to_dict(event)
    if session_key is not None:
        data["session_key"] = session_key
    event_type = type(event).__name__
    # Build a one-line summary for console readability
    summary = ", ".join(f"{k}={v!r}" for k, v in data.items())
    logger.bind(event_type=event_type, **data).log(level, summary)

    # Store in recent event buffer for web UI
    from observability.recent import recent
    recent.add_log(event_type, data)

    # Persist to per-session JSONL file for survival across restarts
    from observability.persistence import store as _persist
    if _persist is not None and session_key:
        try:
            _persist.save_event(session_key, event_type, data)
        except Exception:
            pass  # persistence is best-effort, never crash the caller


def _to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass instance (or nested list of them) to a flat dict."""
    if dataclasses.is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(i) for i in obj]  # type: ignore[return-value]
    return obj
