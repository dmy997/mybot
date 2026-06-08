"""Exponential-backoff retry utilities for LLM API calls."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .errors import (
    ErrorCategory,
    FatalLLMError,
    RecoverableLLMError,
    RetryableLLMError,
)


@dataclass
class RetryConfig:
    """Parameters controlling the retry loop behaviour."""

    max_retries: int = 3
    """Maximum number of retry attempts (not counting the initial call)."""

    base_delay: float = 1.0
    """Initial backoff delay in seconds (doubles each attempt)."""

    max_delay: float = 60.0
    """Absolute ceiling on backoff delay."""

    jitter: bool = True
    """When True, apply ±50% random jitter to avoid thundering-herd."""


def compute_delay(attempt: int, config: RetryConfig) -> float:
    """Return the backoff delay for *attempt* (0-indexed).

    Uses capped exponential backoff with optional jitter:

    ``delay = min(base_delay × 2^attempt, max_delay)``

    When *jitter* is enabled the delay is randomly scaled by 50%-150%.
    """
    delay = min(config.base_delay * (2 ** attempt), config.max_delay)
    if config.jitter:
        delay *= 0.5 + random.random()
    return delay


async def async_retry_sleep(delay: float) -> None:
    """Sleep for *delay* seconds without blocking the event loop."""
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Core retry wrapper
# ---------------------------------------------------------------------------


async def with_retry(
    coro_factory: Any,
    *args: Any,
    classify_error: Any,
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Call *coro_factory* with retry logic.

    Parameters
    ----------
    coro_factory:
        Async callable that returns an awaitable (e.g. ``provider.chat``).
    classify_error:
        Callable ``(Exception) -> LLMErrorInfo`` that categorises errors.
    config:
        Retry parameters.  Uses :class:`RetryConfig` defaults when ``None``.

    Returns
    -------
    The return value of *coro_factory* on success.

    Raises
    ------
    RetryableLLMError
        When all retries are exhausted on a retryable error.
    RecoverableLLMError
        When a recoverable error occurs (no retry attempted).
    FatalLLMError
        When a fatal error occurs (no retry attempted).
    """
    if config is None:
        config = RetryConfig()

    last_error: RetryableLLMError | None = None

    for attempt in range(config.max_retries + 1):  # initial + retries
        try:
            return await coro_factory(*args, **kwargs)
        except (RetryableLLMError, RecoverableLLMError, FatalLLMError):
            raise  # already classified — pass through
        except Exception as exc:
            info = classify_error(exc)

        if info.category == ErrorCategory.RETRYABLE:
            if attempt < config.max_retries:
                delay = info.retry_after or compute_delay(attempt, config)
                logger.warning(
                    "LLM call failed (attempt {}/{}): {}.  Retrying in {:.1f}s...",
                    attempt + 1,
                    config.max_retries + 1,
                    info.message,
                    delay,
                )
                await async_retry_sleep(delay)
                last_error = RetryableLLMError(info)
                continue
            else:
                logger.error(
                    "LLM call failed after {} retries: {}",
                    config.max_retries,
                    info.message,
                )
                raise RetryableLLMError(info)

        elif info.category == ErrorCategory.RECOVERABLE:
            raise RecoverableLLMError(info)

        else:  # FATAL
            raise FatalLLMError(info)

    # All retries exhausted
    assert last_error is not None
    raise last_error
