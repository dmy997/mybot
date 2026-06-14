"""Unified compaction service — three-layer context compression pyramid.

Layer 1 — ``micro_compact`` (rule-based, no LLM, in-memory):
    Clears old tool results beyond *keep_recent_turns*, replacing them with
    a placeholder.  Runs before every ``build_messages()`` call.
    Reference: Claude Code ``microCompact.ts``.

Layer 2 — ``auto_compact`` (LLM summarisation, persistent):
    Triggered when the token budget crosses ``auto_compact_threshold``.
    Calls the LLM to summarise dehydrated messages, appends the summary to
    ``history.jsonl``, and advances ``consolidated_cursor``.
    Has a circuit-breaker (max 3 consecutive failures).
    Reference: Claude Code ``autoCompact.ts`` + ``compact.ts``.

Layer 3 — ``full_compact`` (user-triggered):
    Same as auto_compact but accepts custom summarisation instructions and
    always runs (no circuit breaker gating).
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger

from context.token_budget import TokenBudget
from utils import render_template

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
# Dehydration patterns
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"data:[^;\"\s]*;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)


def dehydrate_messages(
    messages: list[dict[str, Any]],
    max_content_chars: int = 3000,
) -> list[dict[str, Any]]:
    """Strip non-critical payload before sending to the summarisation API.

    - Truncates content strings to *max_content_chars*
    - Replaces base64 data URIs with ``[binary: ...]`` placeholders
    - Drops tool call arguments (keeps function names only)
    """
    dehydrated: list[dict[str, Any]] = []
    for msg in messages:
        d: dict[str, Any] = {}
        for k, v in msg.items():
            if k == "content" and isinstance(v, str):
                v = _DATA_URI_RE.sub("[binary data removed]", v)
                if len(v) > max_content_chars:
                    v = v[:max_content_chars] + (
                        f"\n[... {len(v) - max_content_chars} more chars truncated]"
                    )
                d[k] = v
            elif k == "tool_calls" and isinstance(v, list):
                slim_calls = []
                for tc in v:
                    fn = tc.get("function", {})
                    slim_calls.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": fn.get("name", "?"), "arguments": "{...}"},
                    })
                d[k] = slim_calls
            else:
                d[k] = v
        dehydrated.append(d)
    return dehydrated


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    """Outcome of an auto/full compaction run."""

    compressed_count: int
    """Number of messages compressed (0 = nothing done)."""

    summary: str
    """The generated summary text."""

    was_truncation_fallback: bool = False
    """True if LLM summarisation failed and hard truncation was used."""


# ---------------------------------------------------------------------------
# CompactionService
# ---------------------------------------------------------------------------


class CompactionService:
    """Unified three-layer compaction service.

    Parameters
    ----------
    provider:
        LLM provider for summarisation.  When ``None``, falls back to
        hard truncation.
    token_budget:
        Unified threshold configuration.
    workspace:
        Root directory for sessions/ storage (history.jsonl location).
    session_manager:
        SessionManager instance for cursor advancement.
    compress_model:
        Optional model override for compression calls.
    """

    def __init__(
        self,
        provider: Any | None,
        token_budget: TokenBudget,
        workspace: Path,
        session_manager: SessionManager,
        *,
        compress_model: str | None = None,
    ) -> None:
        self.provider = provider
        self.token_budget = token_budget
        self.workspace = Path(workspace).expanduser().resolve()
        self.session = session_manager
        self.compress_model = compress_model

        self._consecutive_failures = 0

    # ========================================================================
    # Layer 1: Micro-compact (rule-based, no LLM)
    # ========================================================================

    @staticmethod
    def micro_compact(
        messages: list[dict[str, Any]],
        keep_recent_turns: int = 2,
        placeholder: str = "[Old tool result cleared]",
    ) -> list[dict[str, Any]]:
        """Clear tool results older than *keep_recent_turns* turns.

        Tool-calling turns are counted by assistant messages that carry
        ``tool_calls``.  Results from the last *keep_recent_turns* turns
        are kept intact; older ones are replaced with *placeholder*.

        Returns a **new list** — the input is never modified.
        """
        total_turns = sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        cutoff = max(0, total_turns - keep_recent_turns)
        if cutoff == 0:
            return list(messages)

        result: list[dict[str, Any]] = []
        turn = 0
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                turn += 1

            if msg.get("role") == "tool" and turn <= cutoff:
                content = msg.get("content", "")
                if isinstance(content, str) and not content.startswith(placeholder):
                    result.append({**msg, "content": placeholder})
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    # ========================================================================
    # Layer 2: Auto-compact (LLM summarisation, persistent)
    # ========================================================================

    async def auto_compact(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        budget_tokens: int | None = None,
        keep_recent: int | None = None,
        session_memory: Any = None,  # SessionMemory | None
    ) -> CompactionResult:
        """Summarise older messages via LLM and persist to history.jsonl.

        Exactly one of *budget_tokens* or *keep_recent* should be provided:

        - **budget_tokens**: keep as many recent messages as fit within
          this token budget (token-budget compression).
        - **keep_recent**: keep exactly this many messages (idle compression).

        The split point is adjusted to preserve user/assistant turn boundaries.

        When *session_memory* is provided and has sufficient quality
        (score >= 50), the LLM summarisation call is **skipped** and the
        structured notes are used directly as the summary (Path B shortcut).

        Returns :class:`CompactionResult` (count=0 means nothing was done).
        """
        if budget_tokens is not None and keep_recent is not None:
            raise ValueError("Only one of budget_tokens or keep_recent allowed")
        if budget_tokens is None and keep_recent is None:
            raise ValueError("One of budget_tokens or keep_recent is required")

        # Idle compression gating
        if keep_recent is not None and self.token_budget.idle_compress_seconds <= 0:
            return CompactionResult(0, "", was_truncation_fallback=True)

        unsummarised = list(messages)
        if len(unsummarised) <= 1:
            return CompactionResult(0, "", was_truncation_fallback=True)

        # Determine keep count
        if keep_recent is not None:
            keep_count = keep_recent
        else:
            keep_count = self._fit_in_budget(unsummarised, budget_tokens)

        if keep_count >= len(unsummarised):
            return CompactionResult(0, "", was_truncation_fallback=True)

        to_compress = list(unsummarised[:-keep_count])
        to_keep = list(unsummarised[-keep_count:])

        # Adjust split to preserve turn boundaries
        self._adjust_split(to_compress, to_keep)
        if not to_compress:
            return CompactionResult(0, "", was_truncation_fallback=True)

        # Dehydrate + summarise (Path B shortcut if session_memory is quality)
        dehydrated = self._dehydrate_messages(to_compress)
        summary = await self._summarise(dehydrated, session_memory=session_memory)

        # Persist
        self._write_history(session_key, len(to_compress), summary)

        # Advance cursor under lock (session.messages is NOT modified).
        # Use absolute position: `cursor + len(to_compress)` would be wrong
        # when history was capped by max_history_messages (the common case).
        # ``len(session.messages) - len(to_keep)`` correctly computes the
        # start of the un-compressed tail regardless of truncation offset.
        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            old_cursor = session.consolidated_cursor
            new_cursor = len(session.messages) - len(to_keep)
            session.consolidated_cursor = new_cursor
            session.updated_at = datetime.now()
            self.session.save_session(session)

        logger.debug(
            "auto_compact {!r}: {} messages summarised, cursor {} → {}",
            session_key,
            len(to_compress),
            old_cursor,
            new_cursor,
        )

        # Reset circuit breaker on success
        self._consecutive_failures = 0

        return CompactionResult(len(to_compress), summary)

    # ========================================================================
    # Layer 3: Full-compact (user-triggered)
    # ========================================================================

    async def full_compact(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        instructions: str | None = None,
        budget_tokens: int | None = None,
        session_memory: Any = None,  # SessionMemory | None
    ) -> CompactionResult:
        """User-triggered full compaction with optional custom instructions.

        Always runs (bypasses circuit breaker).  If *instructions* is
        provided, they are appended to the summarisation prompt.

        When *session_memory* is provided and has sufficient quality,
        the LLM summarisation call is skipped (Path B shortcut).
        """
        unsummarised = list(messages)
        if len(unsummarised) <= 1:
            return CompactionResult(0, "", was_truncation_fallback=True)

        keep_count = self._fit_in_budget(unsummarised, budget_tokens or 0)
        if keep_count < 1:
            keep_count = 1
        if keep_count >= len(unsummarised):
            return CompactionResult(0, "", was_truncation_fallback=True)

        to_compress = list(unsummarised[:-keep_count])
        to_keep = list(unsummarised[-keep_count:])
        self._adjust_split(to_compress, to_keep)
        if not to_compress:
            return CompactionResult(0, "", was_truncation_fallback=True)

        dehydrated = self._dehydrate_messages(to_compress)
        summary = await self._summarise(
            dehydrated, instructions=instructions, session_memory=session_memory,
        )

        self._write_history(session_key, len(to_compress), summary)

        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            old_cursor = session.consolidated_cursor
            new_cursor = len(session.messages) - len(to_keep)
            session.consolidated_cursor = new_cursor
            session.updated_at = datetime.now()
            self.session.save_session(session)

        self._consecutive_failures = 0

        return CompactionResult(len(to_compress), summary)

    # ========================================================================
    # Circuit breaker
    # ========================================================================

    def can_auto_compact(self) -> bool:
        """Return False when the circuit breaker has tripped."""
        max_failures = self.token_budget.max_consecutive_failures
        if self._consecutive_failures >= max_failures:
            logger.warning(
                "Auto-compact circuit breaker tripped ({} consecutive failures)",
                self._consecutive_failures,
            )
            return False
        return True

    def reset_circuit_breaker(self) -> None:
        """Reset the consecutive failure counter."""
        self._consecutive_failures = 0

    # ========================================================================
    # History summaries (read)
    # ========================================================================

    def read_history_summaries(
        self,
        session_key: str,
        *,
        max_entries: int | None = None,
        max_chars_per_entry: int | None = None,
    ) -> str:
        """Read history.jsonl summaries for system-prompt injection.

        Parameters
        ----------
        max_entries:
            Max number of history entries to include (default from token_budget).
        max_chars_per_entry:
            Max chars per summary entry (default from token_budget).
        """
        max_n = max_entries if max_entries is not None else self.token_budget.max_history_summaries
        max_chars = (
            max_chars_per_entry
            if max_chars_per_entry is not None
            else self.token_budget.max_history_summary_chars
        )

        path = self._history_path(session_key)
        if not path.exists():
            return ""

        entries: list[str] = []
        try:
            all_lines = [
                ln for ln in path.read_text(encoding="utf-8").strip().split("\n")
                if ln.strip()
            ]
            # Take only the last max_n entries
            for line in all_lines[-max_n:]:
                record = _json.loads(line)
                ts = record.get("timestamp", "")[:19]
                summary = record.get("summary", "")
                compressed = record.get("compressed_count", 0)
                if summary:
                    if len(summary) > max_chars:
                        summary = summary[:max_chars] + "\n... (truncated)"
                    entries.append(
                        f"## Historical Summary ({ts}, {compressed} messages)\n\n{summary}"
                    )
        except (_json.JSONDecodeError, OSError):
            return ""

        if not entries:
            return ""

        return (
            "# Previous Conversation Summaries\n\n"
            "The following summaries capture earlier parts of this conversation "
            "that have been archived:\n\n" + "\n\n".join(entries)
        )

    # ========================================================================
    # Internal: history persistence
    # ========================================================================

    def _history_path(self, session_key: str) -> Path:
        return self.workspace / "sessions" / f"{session_key}_history.jsonl"

    def _write_history(
        self, session_key: str, compressed_count: int, summary: str,
    ) -> None:
        """Append a compression record to history.jsonl."""
        path = self._history_path(session_key)
        record = _json.dumps({
            "timestamp": datetime.now().isoformat(),
            "compressed_count": compressed_count,
            "summary": summary,
        }, ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(record + "\n")
        except OSError:
            logger.opt(exception=True).warning(
                "Failed to write history.jsonl for {!r}", session_key,
            )

    # ========================================================================
    # Internal: summarisation
    # ========================================================================

    async def _summarise(
        self, messages: list[dict[str, Any]], *,
        instructions: str | None = None,
        session_memory: Any = None,  # SessionMemory | None
    ) -> str:
        """Summarise *messages*, trying Path B before Path A.

        **Path B** (session-memory shortcut): When *session_memory* has
        quality >= 50 and is fresh, reuse its structured notes directly
        as the summary — saves one LLM API call.

        **Path A** (LLM summarisation): Standard flow — call the LLM to
        summarise dehydrated messages.

        **Fallback**: Hard truncation when provider is unavailable or
        LLM call fails.
        """
        # Path B: reuse session memory notes as summary
        if session_memory is not None and instructions is None:
            if session_memory.is_fresh() and session_memory.has_substance():
                score = session_memory.quality_score()
                if score >= 50:
                    notes_summary = session_memory.get_compact_summary()
                    if notes_summary.strip():
                        logger.info(
                            "Path B: using session memory as compression summary "
                            "(quality={}, saved 1 LLM call)", score,
                        )
                        self._consecutive_failures = 0
                        return notes_summary
                    logger.debug(
                        "Path B skipped: notes summary empty (quality={})", score,
                    )
                else:
                    logger.debug(
                        "Path B skipped: quality {} < 50", score,
                    )

        # Path A: LLM summarisation
        if self.provider is None:
            return self._truncate_summary(messages)
        try:
            return await self._llm_summarise(messages, instructions=instructions)
        except Exception:
            logger.opt(exception=True).warning(
                "LLM summarisation failed, falling back to truncation"
            )
            self._consecutive_failures += 1
            return self._truncate_summary(messages)

    @staticmethod
    def _truncate_summary(
        messages: list[dict[str, Any]], *, max_chars: int = 2000,
    ) -> str:
        """Hard-truncation fallback when the LLM summarisation API is unavailable."""
        if not messages:
            return "(empty context)"

        parts: list[str] = []
        total = 0
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            content = str(content)
            is_edge = (msg is messages[0] or msg is messages[-1])
            limit = 300 if is_edge else 150
            if len(content) > limit:
                content = content[:limit] + "..."
            line = f"[{role}] {content}" if content.strip() else f"[{role}] (no content)"
            if total + len(line) > max_chars:
                parts.append(f"[+{len(messages) - len(parts)} more messages truncated]")
                break
            parts.append(line)
            total += len(line)

        return "\n".join(parts) if parts else "(empty context)"

    async def _llm_summarise(
        self, messages: list[dict[str, Any]], *,
        instructions: str | None = None,
    ) -> str:
        """Use a lightweight LLM call to summarise messages.

        Messages should already be dehydrated before calling this method.
        """
        trunc_len = self.token_budget.content_truncate_length
        slim: list[dict[str, Any]] = [
            {**m, "content": (
                m.get("content", "")[:trunc_len] + "..."
                if isinstance(m.get("content", ""), str)
                and len(m.get("content", "")) > trunc_len
                else m.get("content", "")
            )}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]

        if not slim:
            return "(empty context)"

        summary_prompt = render_template(
            "context/summary.md",
            max_words=self.token_budget.summary_max_words,
            strip=True,
        )
        if instructions:
            summary_prompt += f"\n\nAdditional instructions: {instructions}"
        slim.append({"role": "user", "content": summary_prompt})

        response = await self.provider.chat_with_retry(
            messages=slim,
            tools=[],
            model=self.compress_model,
            max_tokens=300,
            temperature=0.0,
        )
        return response.content or "(summarisation produced no output)"

    # ========================================================================
    # Internal: dehydration
    # ========================================================================

    def _dehydrate_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Strip non-critical payload before sending to the summarisation API.

        Delegates to the module-level :func:`dehydrate_messages` with the
        configured ``dehydrate_max_content_chars``.
        """
        return dehydrate_messages(
            messages,
            max_content_chars=self.token_budget.dehydrate_max_content_chars,
        )

    # Backward-compat static entry point for tests
    @staticmethod
    def _dehydrate_messages_static(
        messages: list[dict[str, Any]],
        max_content_chars: int = 3000,
    ) -> list[dict[str, Any]]:
        """Static wrapper for tests that don't have a CompactionService instance."""
        return dehydrate_messages(messages, max_content_chars=max_content_chars)

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
