"""Consolidator — lightweight token-budget-triggered conversation summarization.

After each conversation turn, the Consolidator checks whether the session's
unconsolidated messages exceed the safe token budget.  If they do, it
summarizes old message chunks via LLM and appends them to ``history.jsonl``.

Consolidation runs **asynchronously** (fire-and-forget via ``asyncio.create_task``)
so it never blocks the user-facing response.  Per-session ``asyncio.Lock``
prevents concurrent consolidation on the same session.

Reference: nanobot ``agent/memory.py`` Consolidator class.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from utils import render_template

if TYPE_CHECKING:
    from memory.store import MemoryStore
    from providers.base import LLMProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary cap
_MAX_CONSOLIDATION_ROUNDS = 5
_SAFETY_BUFFER = 1024                 # extra headroom for token estimation drift


class Consolidator:
    """Summarizes conversation messages into history.jsonl when the prompt
    exceeds a configurable fraction of the context window.

    Parameters
    ----------
    store:
        The :class:`MemoryStore` for history I/O.
    provider:
        The LLM provider used to produce summaries (cheap model preferred).
    model:
        The model name used for consolidation.
    context_window_tokens:
        Total context window size of the main model.
    consolidation_ratio:
        Target fraction of the context window after consolidation
        (default 0.7, i.e. 70%).
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider | None = None,
        model: str = "",
        *,
        context_window_tokens: int = 128_000,
        consolidation_ratio: float = 0.7,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.consolidation_ratio = consolidation_ratio
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        """Return (and cache) a per-session asyncio.Lock."""
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]

    # -- public API -----------------------------------------------------------

    async def maybe_consolidate(
        self,
        session: Any,
        build_messages_fn: Any | None = None,
    ) -> bool:
        """Check token budget and consolidate if over threshold.

        Safe to call from fire-and-forget tasks — uses a per-session
        ``asyncio.Lock`` to serialise concurrent consolidations.

        Returns True if consolidation was performed.
        """
        if self.context_window_tokens <= 0 or self.provider is None:
            return False

        session_key = getattr(session, "key", "default")
        lock = self._get_lock(session_key)
        async with lock:
            return await self._do_consolidate(session, build_messages_fn)

    async def _do_consolidate(
        self,
        session: Any,
        build_messages_fn: Any | None = None,
    ) -> bool:
        """Core consolidation logic (called under lock)."""
        messages = session.messages
        if not messages:
            return False

        # Count unconsolidated messages
        last_consolidated = getattr(session, "last_consolidated", 0)
        unconsolidated = messages[last_consolidated:]
        if not unconsolidated:
            return False

        session_key = getattr(session, "key", "")

        # Token estimation
        if build_messages_fn is not None:
            try:
                assembled = await build_messages_fn()
                estimated = self._estimate_tokens(assembled)
            except Exception:
                logger.exception("Token estimation failed, using fallback")
                estimated = self._estimate_tokens(unconsolidated)
        else:
            estimated = self._estimate_tokens(unconsolidated)

        budget = self._input_token_budget
        target = int(budget * self.consolidation_ratio)

        if estimated < budget:
            logger.debug(
                "Consolidation idle: estimated={}/{}, msgs={}",
                estimated, budget, len(unconsolidated),
            )
            return False

        logger.info(
            "Consolidation triggered: estimated={}/{}, msgs={}",
            estimated, budget, len(unconsolidated),
        )

        for round_num in range(_MAX_CONSOLIDATION_ROUNDS):
            if estimated <= target:
                break

            # Pick a chunk: use a simple approach — archive half of unconsolidated
            boundary = self._pick_boundary(unconsolidated, max(1, estimated - target))
            if boundary is None or boundary <= 0:
                break

            chunk = unconsolidated[:boundary]
            if not chunk:
                break

            logger.info(
                "Consolidation round {}: chunk={} msgs, estimated={}",
                round_num, len(chunk), estimated,
            )
            await self.archive(chunk, session_key=session_key)

            # Advance the consolidation cursor
            last_consolidated += boundary
            session.last_consolidated = last_consolidated
            unconsolidated = messages[last_consolidated:]

            if not unconsolidated:
                break
            # Re-estimate after archiving
            if build_messages_fn is not None:
                try:
                    assembled = await build_messages_fn()
                    estimated = self._estimate_tokens(assembled)
                except Exception:
                    estimated = self._estimate_tokens(unconsolidated)
            else:
                estimated = self._estimate_tokens(unconsolidated)

        return True

    async def archive(self, messages: list[dict],
                      session_key: str = "",
                      instructions: str | None = None) -> str | None:
        """Summarize *messages* via LLM and append to history.jsonl.

        When *instructions* is provided, it is appended to the system prompt
        to guide the summarisation (used by user-triggered full compaction).

        Returns the summary text on success, or None.
        """
        if not messages:
            return None

        try:
            formatted = self._format_messages(messages)
            system_prompt = render_template(
                "agent/consolidator_archive.md",
                strip=True,
            )
            if instructions:
                system_prompt = (
                    f"{system_prompt}\n\n"
                    f"Additional user instructions for this summary:\n"
                    f"{instructions}"
                )
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": formatted},
                ],
                tools=[],
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            self.store.append_history(summary, max_chars=_ARCHIVE_SUMMARY_MAX_CHARS,
                                      session_key=session_key)
            return summary
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-archiving")
            self.store.raw_archive(messages, session_key=session_key)
            return None

    # -- helpers --------------------------------------------------------------

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for the main LLM."""
        return self.context_window_tokens - _SAFETY_BUFFER

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Rough token estimate: ~4 chars per token."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += max(0, len(content) // 4)
            elif isinstance(content, list):
                # Multi-modal content array
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += max(0, len(part.get("text", "")) // 4)
        return total

    @staticmethod
    def _pick_boundary(
        messages: list[dict],
        target_tokens: int,
    ) -> int | None:
        """Pick a user-turn boundary in *messages* after roughly *target_tokens*.

        Returns the index of the first message to archive.
        """
        accumulated = 0
        last_user_idx: int | None = None

        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            msg_tokens = len(content) // 4 if isinstance(content, str) else 0
            accumulated += msg_tokens

            if msg.get("role") == "user":
                last_user_idx = i
                if accumulated >= target_tokens:
                    return i + 1  # include up to and including this user turn

        return last_user_idx + 1 if last_user_idx is not None else None

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        """Format messages for the consolidation LLM."""
        lines = []
        for msg in messages:
            if not msg.get("content"):
                continue
            role = msg.get("role", "?").upper()
            content = str(msg["content"])
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)
