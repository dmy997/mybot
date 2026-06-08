"""Structured error types for LLM API error handling.

Categorises errors into three classes so callers can decide whether to
retry, recover, or abort.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ErrorCategory(enum.Enum):
    RETRYABLE = "retryable"
    """Transient error — safe to retry with backoff."""

    RECOVERABLE = "recoverable"
    """Permanent error — can be mitigated by adjusting request parameters."""

    FATAL = "fatal"
    """Permanent error — request cannot succeed, abort immediately."""


@dataclass
class LLMErrorInfo:
    """Structured error payload produced by :meth:`LLMProvider._classify_error`."""

    category: ErrorCategory
    message: str
    status_code: int | None = None
    error_type: str | None = None
    """Machine-readable label, e.g. ``"rate_limit"``, ``"context_length"``,
    ``"auth_error"``, ``"content_filter"``."""

    retry_after: float | None = None
    """Seconds to wait before retrying (from ``Retry-After`` header)."""

    raw_error: BaseException | None = None
    """The original exception, preserved for logging or debugging."""


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class RetryableLLMError(Exception):
    """Transient error that should be retried with exponential backoff."""

    def __init__(self, info: LLMErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


class RecoverableLLMError(Exception):
    """Permanent error that may be mitigated by adjusting the request."""

    def __init__(self, info: LLMErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


class FatalLLMError(Exception):
    """Permanent error — do not retry, propagate to the user."""

    def __init__(self, info: LLMErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info
