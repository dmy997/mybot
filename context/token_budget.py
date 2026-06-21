"""Unified token budget configuration.

All compaction thresholds, truncation limits, and circuit-breaker settings
live here so ContextManager and AgentCore read from a single source.

Reference: Claude Code's ``autoCompact.ts`` multi-threshold system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_WINDOW = 200_000
_DEFAULT_MAX_OUTPUT_TOKENS = 20_000
_DEFAULT_AUTOCOMPACT_BUFFER = 13_000   # reserve for output
_DEFAULT_WARNING_BUFFER = 20_000
_DEFAULT_BLOCK_BUFFER = 3_000


@dataclass
class TokenBudget:
    """Single source of truth for all token thresholds.

    All fields are configurable via constructor kwargs.  Sensible defaults
    are provided for a ~200K context-window model (DeepSeek V4, GPT-4o, etc.).
    """

    # -- window sizing ---------------------------------------------------------

    context_window: int = _DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS

    # -- four-tier thresholds (computed) ---------------------------------------

    @property
    def effective_window(self) -> int:
        """Usable context after reserving space for the model response."""
        return self.context_window - min(self.max_output_tokens, 20_000)

    @property
    def warning_threshold(self) -> int:
        """Warn the user that context is getting full."""
        return self.effective_window - _DEFAULT_WARNING_BUFFER

    @property
    def auto_compact_threshold(self) -> int:
        """Trigger automatic LLM summarisation."""
        return self.effective_window - _DEFAULT_AUTOCOMPACT_BUFFER

    @property
    def block_threshold(self) -> int:
        """Refuse to proceed until compaction runs."""
        return self.effective_window - _DEFAULT_BLOCK_BUFFER

    # -- truncation limits (unified — was scattered across 3 locations) --------

    tool_result_max_chars: int = 6_000
    """Cap for *new* tool results stored in the session (AgentCore)."""

    history_tool_result_max_chars: int = 4_000
    """Cap for tool results loaded from session history (ContextManager)."""

    tool_call_args_max_chars: int = 10_000
    """Per-value cap inside ``tool_calls[].function.arguments``."""

    # -- micro-compact ---------------------------------------------------------

    micro_compact_keep_turns: int = 2
    """Number of recent tool-calling turns to keep intact."""

    micro_compact_placeholder: str = "[Old tool result cleared]"

    # -- compression ratio -----------------------------------------------------

    compress_ratio: float = 0.5
    """Fraction of max_context_tokens to reserve for recent messages."""

    # -- history loading -------------------------------------------------------

    max_history_messages: int = 100
    """Max raw messages to load from session history."""

    # -- idle compression ------------------------------------------------------

    idle_compress_seconds: int = 300
    """Compress session after this many seconds of inactivity (0 = disabled)."""
