"""Unified compaction service — cursor-based context compression.

Layer 1 — ``micro_compact`` (rule-based, no LLM, in-memory):
    Clears old tool results beyond *keep_recent_turns*, replacing them with
    a placeholder.  Runs before every ``build_messages()`` call.
    Reference: Claude Code ``microCompact.ts``.

Layer 2 — ``auto_compact`` (cursor advancement, persistent):
    Triggered when the token budget crosses ``auto_compact_threshold``.
    Advances ``consolidated_cursor`` so older messages are skipped on the
    next build.  No LLM summarisation — that is handled by Consolidator
    writing to the global ``memory/history.jsonl``.
    Reference: Claude Code ``autoCompact.ts`` + ``compact.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

from loguru import logger

from context.token_budget import TokenBudget

if TYPE_CHECKING:
    from context.session import SessionManager

# ---------------------------------------------------------------------------
# Token estimation (lightweight)
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        try:
            return len(_ENC.encode(text))
        except Exception:
            return len(text) // 4
except ImportError:
    def _count_tokens(text: str) -> int:  # type: ignore[no-redef]
        return len(text) // 4


def _estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count for a list of chat messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _count_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += _count_tokens(part["text"])
        if "tool_calls" in msg:
            total += _count_tokens(str(msg["tool_calls"]))
        total += 4
    return total


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    """Outcome of an auto/full compaction run."""

    compressed_count: int
    """Number of messages compressed (0 = nothing done)."""


# ---------------------------------------------------------------------------
# CompactionService
# ---------------------------------------------------------------------------


class CompactionService:
    """Two-layer compaction service.

    Cursor advancement is the only persistence — older messages are skipped
    on the next :meth:`ContextManager.build_messages` call.  Summarisation is
    handled by :class:`Consolidator` writing to the global history.jsonl.

    Parameters
    ----------
    token_budget:
        Unified threshold configuration.
    session_manager:
        SessionManager instance for cursor advancement.
    """

    def __init__(
        self,
        token_budget: TokenBudget,
        session_manager: SessionManager,
    ) -> None:
        self.token_budget = token_budget
        self.session = session_manager

    # ========================================================================
    # Layer 1: Micro-compact (rule-based, no LLM)
    # ========================================================================

    @staticmethod
    def micro_compact(
        messages: list[dict[str, Any]],
        keep_recent_turns: int = 2,
        placeholder: str = "[Old tool result cleared]",
    ) -> list[dict[str, Any]]:
        """Compact messages in three rule-based steps (no LLM).

        1. Clear tool results older than *keep_recent_turns* turns.
        2. Remove orphan tool results (no matching tool_call_id).
        3. Fill missing tool results (tool_call with no result).

        Returns a **new list** — the input is never modified.
        """
        # Step 1: Clear old tool results
        total_turns = sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        cutoff = max(0, total_turns - keep_recent_turns)

        if cutoff == 0:
            cleaned = list(messages)
        else:
            cleaned: list[dict[str, Any]] = []
            turn = 0
            for msg in messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    turn += 1

                if msg.get("role") == "tool" and turn <= cutoff:
                    content = msg.get("content", "")
                    if isinstance(content, str) and not content.startswith(placeholder):
                        cleaned.append({**msg, "content": placeholder})
                    else:
                        cleaned.append(msg)
                else:
                    cleaned.append(msg)

        # Step 2: Remove orphan tool results (no matching tool_call)
        valid_ids: set[str] = set()
        for msg in cleaned:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and "id" in tc:
                        valid_ids.add(tc["id"])
        cleaned = [
            m for m in cleaned
            if m.get("role") != "tool" or m.get("tool_call_id") in valid_ids
        ]

        # Step 3: Fill missing tool results
        result_ids: set[str] = set()
        for msg in cleaned:
            if msg.get("role") == "tool":
                result_ids.add(msg.get("tool_call_id", ""))

        final: list[dict[str, Any]] = []
        for msg in cleaned:
            final.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else ""
                    if tc_id and tc_id not in result_ids:
                        final.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[Tool result unavailable — compacted]",
                        })
                        result_ids.add(tc_id)

        return final

    # ========================================================================
    # Layer 2: Auto-compact (cursor advancement, no LLM)
    # ========================================================================

    async def auto_compact(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        budget_tokens: int | None = None,
        keep_recent: int | None = None,
        consolidator: Any | None = None,
    ) -> CompactionResult:
        """Advance consolidated_cursor to skip older messages.

        Exactly one of *budget_tokens* or *keep_recent* should be provided:

        - **budget_tokens**: keep as many recent messages as fit within
          this token budget (token-budget compression).
        - **keep_recent**: keep exactly this many messages (idle compression).

        When *keep_recent* is used and *consolidator* is provided, the older
        messages are LLM-summarised via :meth:`consolidator.archive` and
        written to the global ``memory/history.jsonl`` **before** the cursor
        is advanced.

        The split point is adjusted to preserve user/assistant turn boundaries.

        Returns :class:`CompactionResult` (count=0 means nothing was done).
        """
        if budget_tokens is not None and keep_recent is not None:
            raise ValueError("Only one of budget_tokens or keep_recent allowed")
        if budget_tokens is None and keep_recent is None:
            raise ValueError("One of budget_tokens or keep_recent is required")

        # Idle compression gating
        if keep_recent is not None and self.token_budget.idle_compress_seconds <= 0:
            return CompactionResult(0)

        unsummarised = list(messages)
        if len(unsummarised) <= 1:
            return CompactionResult(0)

        # Determine keep count
        if keep_recent is not None:
            keep_count = keep_recent
        else:
            keep_count = self._fit_in_budget(unsummarised, budget_tokens)

        if keep_count >= len(unsummarised):
            return CompactionResult(0)

        to_compress = list(unsummarised[:-keep_count])
        to_keep = list(unsummarised[-keep_count:])

        # Adjust split to preserve turn boundaries
        self._adjust_split(to_compress, to_keep)
        if not to_compress:
            return CompactionResult(0)

        # Idle path: archive old messages via Consolidator before advancing cursor
        if keep_recent is not None and consolidator is not None:
            try:
                await consolidator.archive(to_compress, session_key=session_key)
            except Exception:
                logger.opt(exception=True).warning(
                    "Consolidator.archive failed during idle compression for {!r}",
                    session_key,
                )

        # Advance cursor under lock (session.messages is NOT modified)
        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            old_cursor = session.consolidated_cursor
            new_cursor = len(session.messages) - len(to_keep)
            session.consolidated_cursor = new_cursor
            session.updated_at = datetime.now()
            self.session.save_session(session)

        logger.debug(
            "auto_compact {!r}: {} messages compressed, cursor {} → {}",
            session_key,
            len(to_compress),
            old_cursor,
            new_cursor,
        )

        return CompactionResult(len(to_compress))

    # ========================================================================
    # Full-compact (user-triggered, same logic as auto_compact)
    # ========================================================================

    async def full_compact(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        instructions: str | None = None,
        budget_tokens: int | None = None,
        consolidator: Any | None = None,
    ) -> CompactionResult:
        """User-triggered full compaction.

        Always runs (no circuit breaker).  LLM-summarises old messages via
        :class:`Consolidator.archive` (when *consolidator* is provided), then
        advances the consolidated cursor.  The *instructions* parameter is
        passed through to the LLM summarisation prompt.
        """
        unsummarised = list(messages)
        if len(unsummarised) <= 1:
            return CompactionResult(0)

        keep_count = self._fit_in_budget(unsummarised, budget_tokens or 0)
        if keep_count < 1:
            keep_count = 1
        if keep_count >= len(unsummarised):
            return CompactionResult(0)

        to_compress = list(unsummarised[:-keep_count])
        to_keep = list(unsummarised[-keep_count:])
        self._adjust_split(to_compress, to_keep)
        if not to_compress:
            return CompactionResult(0)

        # LLM-summarise old messages before discarding them
        if consolidator is not None:
            try:
                await consolidator.archive(
                    to_compress,
                    session_key=session_key,
                    instructions=instructions,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "Consolidator.archive failed during full compaction for {!r}",
                    session_key,
                )

        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            old_cursor = session.consolidated_cursor
            new_cursor = len(session.messages) - len(to_keep)
            session.consolidated_cursor = new_cursor
            session.updated_at = datetime.now()
            self.session.save_session(session)

        logger.debug(
            "full_compact {!r}: {} messages compressed, cursor {} → {}",
            session_key,
            len(to_compress),
            old_cursor,
            new_cursor,
        )

        return CompactionResult(len(to_compress))

    # ========================================================================
    # Internal: split helpers
    # ========================================================================

    @staticmethod
    def _fit_in_budget(
        messages: list[dict[str, Any]], budget_tokens: int,
    ) -> int:
        """Count how many messages from the end fit within *budget_tokens*."""
        count = 0
        tokens = 0
        for msg in reversed(messages):
            t = _estimate_message_tokens([msg])
            if tokens + t > budget_tokens:
                break
            count += 1
            tokens += t
        return max(count, 1)

    @staticmethod
    def _adjust_split(
        to_compress: list[dict[str, Any]],
        to_keep: list[dict[str, Any]],
    ) -> None:
        """Move messages from *to_compress* to *to_keep* to avoid splitting turns.

        Adjusts so that *to_keep* starts on a clean boundary:
        - "user" → safe (start of a new exchange)
        - "assistant" with tool_calls → safe (tool results follow in to_keep)
        - "assistant" without tool_calls → move preceding "user" into to_keep
        - "tool" → move preceding "assistant" (with tool_calls) into to_keep
        """
        while to_compress and to_keep:
            first_keep = to_keep[0]
            role = first_keep.get("role", "")

            if role == "tool":
                prev = to_compress[-1]
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    to_keep.insert(0, to_compress.pop())
                else:
                    break
            elif role == "assistant" and not first_keep.get("tool_calls"):
                prev = to_compress[-1]
                if prev.get("role") == "user":
                    to_keep.insert(0, to_compress.pop())
                else:
                    break
            else:
                break
