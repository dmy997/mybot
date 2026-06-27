"""ContextManager — unified context assembly, compression, and session persistence.

Owns the full lifecycle of "what the LLM sees":
1. Interrupted-session repair (unmatched tool calls, missing assistant responses)
2. System prompt assembly (base + memory + skills + tools + history summaries)
3. Session history loading (cursor-based, capped, turn-boundary-safe)
4. Token-budget check with multi-threshold compaction gating
5. Memory read (context injection) and write (persisting exchange excerpts)

Compression is delegated to :class:`CompactionService` (three-layer pyramid).
Thresholds are read from :class:`TokenBudget` (single configuration source).
"""

from __future__ import annotations

import json as _json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

from context.compaction import (
    CompactionService,
    _estimate_message_tokens,
)
from context.token_budget import TokenBudget
from memory.consolidator import Consolidator
from memory.store import MemoryStore
from tools import ToolRegistry
from utils import render_template

from .session import SessionManager

# Re-export for backward compatibility (used by core/runner.py)
__all__ = [
    "ContextManager",
    "_estimate_message_tokens",
    "CompactionService",
    "TokenBudget",
]

# ---------------------------------------------------------------------------
# Default base system prompt — always included unless overridden by caller
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = (
    "Reply in the same language the user is using. "
    "If the user writes in Chinese, reply in Chinese. "
    "If the user writes in English, reply in English. "
    "Follow the SOUL.md identity and USER.md profile unless they conflict "
    "with the language of the current conversation — the user's message "
    "language always takes priority."
)

# ---------------------------------------------------------------------------
# Module-level helpers (kept here — used by build_messages and runner.py)
# ---------------------------------------------------------------------------


def _truncate_tool_results(
    messages: list[dict[str, Any]], max_chars: int,
) -> list[dict[str, Any]]:
    """Cap ``content`` of tool-result messages to *max_chars*."""
    return [
        {
            **m,
            "content": (
                m["content"][:max_chars] + "\n... (truncated)"
                if isinstance(m.get("content"), str)
                and len(m["content"]) > max_chars
                else m.get("content", "")
            ),
        }
        if m.get("role") == "tool"
        else m
        for m in messages
    ]


def _truncate_tool_call_args(
    messages: list[dict[str, Any]], max_arg_chars: int,
) -> list[dict[str, Any]]:
    """Cap per-value string sizes inside ``tool_calls[].function.arguments``."""
    import json as _json_local

    result: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "assistant":
            result.append(m)
            continue
        tcs = m.get("tool_calls")
        if not tcs or not isinstance(tcs, list):
            result.append(m)
            continue

        trimmed: list[dict[str, Any]] = []
        for tc in tcs:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
            if fn is None:
                trimmed.append(tc)
                continue
            raw_args = fn.get("arguments", "")
            if not isinstance(raw_args, str) or len(raw_args) <= max_arg_chars:
                trimmed.append(tc)
                continue

            try:
                args_obj = _json_local.loads(raw_args)
                if isinstance(args_obj, dict):
                    for k in list(args_obj.keys()):
                        v = args_obj[k]
                        if isinstance(v, str) and len(v) > max_arg_chars:
                            args_obj[k] = v[:max_arg_chars] + "\n... [truncated]"
                    new_args = _json_local.dumps(args_obj, ensure_ascii=False)
                else:
                    new_args = raw_args[:max_arg_chars] + "\n... [truncated]"
            except (_json_local.JSONDecodeError, TypeError):
                new_args = raw_args[:max_arg_chars] + "\n... [truncated]"

            trimmed.append({
                **tc,
                "function": {**fn, "arguments": new_args},
            })

        result.append({**m, "tool_calls": trimmed})
    return result


# ---------------------------------------------------------------------------
# Constants (session repair)
# ---------------------------------------------------------------------------

_INTERRUPT_MESSAGE = "Error: Task interrupted before a response was generated."
_INTERRUPT_TOOL_RESULT = "Error: Tool execution interrupted."


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class ContextManager:
    """Unified context management for the agent framework.

    Parameters
    ----------
    workspace:
        Root directory for sessions/ and memory/ storage.
    provider:
        Optional LLM provider for summarisation-based compression.
    system_prompt:
        Base system prompt prepended to every request.
    max_context_tokens:
        Soft token budget (used to initialise :class:`TokenBudget`).
    idle_compress_seconds:
        When a session has been idle longer than this, summarise older
        messages on the next access.  Set to ``0`` to disable.
    compress_model:
        Optional model override for compression calls.
    compress_ratio:
        Fraction of ``max_context_tokens`` to reserve for recent messages
        during token-budget compression.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        provider: Any | None = None,
        system_prompt: str = "",
        max_context_tokens: int = 200_000,
        idle_compress_seconds: int = 300,
        compress_model: str | None = None,
        compress_ratio: float = 0.5,
        disabled_skills: list[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.system_prompt = system_prompt

        from core.skills import SkillsLoader as _SkillsLoader

        self.session = SessionManager(self.workspace)
        self.store = MemoryStore(self.workspace)
        self.skills_loader = _SkillsLoader(self.workspace, disabled_skills=disabled_skills)

        # Consolidator — per-turn LLM summarization → history.jsonl
        self.consolidator = Consolidator(
            store=self.store,
            provider=provider,
            model=compress_model or "",
            context_window_tokens=max_context_tokens,
            consolidation_ratio=compress_ratio,
        )

        # Unified token budget (must be first — used by memory_service below)
        self.token_budget = TokenBudget(
            context_window=max_context_tokens,
            compress_ratio=compress_ratio,
            idle_compress_seconds=idle_compress_seconds,
        )

        # Unified compaction service (cursor advancement, no LLM)
        self.compaction = CompactionService(
            token_budget=self.token_budget,
            session_manager=self.session,
        )

        # Three-layer partitioned prompt cache:
        #
        # Layer 1 — static: base prompt + skills + tools.
        #   Rebuilt once on first use; invalidated only when tools change.
        #
        # Layer 2 — memory context: SOUL, USER, MEMORY index, relevant entries.
        #   Keyed by (session, query_bucket) so similar queries reuse results.
        #   Invalidated on remember() / forget().
        #
        # Dynamic parts (file context, recent history) are never cached and
        # always computed fresh.
        self._static_prompt: str | None = None
        self._memory_cache: dict[str, str] = {}    # key: "session:query_bucket"
        self._memory_cache_max = 50

        self._provider = provider
        self._compress_model = compress_model

    # -- properties (sync with token_budget) --------------------------------

    @property
    def provider(self) -> Any | None:
        return self._provider

    @provider.setter
    def provider(self, value: Any | None) -> None:
        self._provider = value

    @property
    def max_context_tokens(self) -> int:
        return self.token_budget.context_window

    @max_context_tokens.setter
    def max_context_tokens(self, value: int) -> None:
        self.token_budget.context_window = value

    @property
    def compress_ratio(self) -> float:
        return self.token_budget.compress_ratio

    @compress_ratio.setter
    def compress_ratio(self, value: float) -> None:
        self.token_budget.compress_ratio = value

    @property
    def idle_compress_seconds(self) -> int:
        return self.token_budget.idle_compress_seconds

    @idle_compress_seconds.setter
    def idle_compress_seconds(self, value: int) -> None:
        self.token_budget.idle_compress_seconds = value

    @property
    def compress_model(self) -> str | None:
        return self._compress_model

    @compress_model.setter
    def compress_model(self, value: str | None) -> None:
        self._compress_model = value

    # ========================================================================
    # Public API
    # ========================================================================

    # -- message assembly -----------------------------------------------------

    async def build_messages(
        self,
        session_key: str,
        current_input: str,
        *,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Assemble the complete message list for an agent run.

        Composition order:
        1. Repair interrupted session
        2. Micro-compact old tool results (Layer 1 — rule-based, no LLM)
        3. System prompt (base + memory + skills + tools + history summaries)
        4. Session history from ``consolidated_cursor`` onwards (capped)
        5. Current user input
        6. Multi-threshold token-budget check:
           - > block_threshold → force auto_compact
           - > auto_compact_threshold → auto_compact (with circuit breaker)
           - > warning_threshold → logger warning

        Returns a list ready for ``AgentInput.init_messages``.
        """
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor

        # 1. Load raw history (non-destructive repair — does NOT modify stored session)
        raw_history, _ = self._repair_messages(session.messages)
        raw_history = raw_history[cursor:]
        raw_history = raw_history[-self.token_budget.max_history_messages:]

        # 2. Micro-compact: clear old tool results (Layer 1)
        history = self.compaction.micro_compact(
            raw_history,
            keep_recent_turns=self.token_budget.micro_compact_keep_turns,
            placeholder=self.token_budget.micro_compact_placeholder,
        )

        # 3. Filter out system messages
        history = [m for m in history if m.get("role") != "system"]

        # 4. Cap tool-result / tool-arg sizes in history
        history = _truncate_tool_results(
            history, self.token_budget.history_tool_result_max_chars,
        )
        history = _truncate_tool_call_args(
            history, self.token_budget.tool_call_args_max_chars,
        )

        # 5. Build system prompt (includes capped history summaries + memory relevance)
        system_content = await self._build_system_prompt(
            session_key, tools=tools, skills=skills, query=current_input,
            messages=history,
        )

        # 6. Assemble preliminary list
        preliminary: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ] + history + [
            {"role": "user", "content": current_input},
        ]

        # 7. Multi-threshold token-budget check
        budget = self.token_budget
        if budget.context_window <= 0:
            return preliminary

        total = _estimate_message_tokens(preliminary)

        # Block threshold: force compaction
        if total > budget.block_threshold:
            logger.warning(
                "Context at {} tokens exceeds block threshold ({}), forcing compaction",
                total, budget.block_threshold,
            )
            result = await self.compaction.auto_compact(
                session_key, history,
                budget_tokens=int(budget.effective_window * budget.compress_ratio),
            )
            if result.compressed_count > 0:
                return await self._rebuild_after_compact(
                    session_key, current_input, tools, skills,
                )

        # Auto-compact threshold
        elif total > budget.auto_compact_threshold:
            logger.info(
                "Context at {} tokens exceeds auto-compact threshold ({}), compacting",
                total, budget.auto_compact_threshold,
            )
            result = await self.compaction.auto_compact(
                session_key, history,
                budget_tokens=int(budget.effective_window * budget.compress_ratio),
            )
            if result.compressed_count > 0:
                return await self._rebuild_after_compact(
                    session_key, current_input, tools, skills,
                )

        # Warning threshold
        elif total > budget.warning_threshold:
            logger.warning(
                "Context at {} tokens ({}% of window), consider compacting",
                total, total / budget.effective_window * 100,
            )

        return preliminary

    # -- unified compression (thin wrapper, backward-compatible) ---------------

    async def compress(
        self,
        session_key: str,
        *,
        keep_recent: int | None = None,
        budget_tokens: int | None = None,
    ) -> int:
        """Compress unsummarised messages (delegates to :class:`CompactionService`).

        Exactly one of *keep_recent* or *budget_tokens* must be provided.

        When *keep_recent* (idle compression) is used, the Consolidator is
        invoked to LLM-summarise the older messages into the global
        ``memory/history.jsonl`` before the cursor is advanced.

        Returns the number of messages compressed (0 if nothing was done).
        """
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor
        unsummarised = session.messages[cursor:]

        result = await self.compaction.auto_compact(
            session_key, unsummarised,
            budget_tokens=budget_tokens,
            keep_recent=keep_recent,
            consolidator=self.consolidator,
        )
        return result.compressed_count

    async def full_compress(
        self,
        session_key: str,
        *,
        instructions: str | None = None,
        budget_tokens: int | None = None,
    ) -> int:
        """User-triggered full compaction with LLM summarisation.

        Unlike :meth:`compress` (which may idle-gate), this always runs.
        Old messages are LLM-summarised via the Consolidator into
        ``memory/history.jsonl``, then the consolidated cursor is advanced.

        The *instructions* parameter is injected into the LLM summarisation
        prompt to guide what the summary should focus on.

        Returns the number of messages compressed (0 if nothing was done).
        """
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor
        unsummarised = session.messages[cursor:]

        result = await self.compaction.full_compact(
            session_key, unsummarised,
            instructions=instructions,
            budget_tokens=budget_tokens,
            consolidator=self.consolidator,
        )
        return result.compressed_count

    # -- session lifecycle ----------------------------------------------------

    async def save_exchange(
        self,
        session_key: str,
        user_input: str,
        assistant_messages: list[dict[str, Any]],
        *,
        tools_used: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Append a user+assistant exchange to the session log."""
        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            session.messages.append({"role": "user", "content": user_input})
            for msg in assistant_messages:
                session.messages.append(msg)
            session.updated_at = datetime.now()
            self.session.save_session(session)

    async def save_session(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist the full message list after an agent run.

        Prefer :meth:`save_exchange` for normal use.
        """
        async with self.session.lock_session(session_key):
            self.session.set_messages(session_key, messages)

    def get_history(self, session_key: str) -> list[dict[str, Any]]:
        """Return session messages without the system prompt."""
        messages = self.session.get_session_history(session_key)
        return [m for m in messages if m.get("role") != "system"]

    def delete_session(self, session_key: str) -> bool:
        """Delete a session from disk and memory."""
        return self.session.delete_session(session_key)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.session.list_sessions()

    # ========================================================================
    # Session memory (per-conversation structured notes)
    # ========================================================================

    # ========================================================================
    # System prompt assembly
    # ========================================================================

    async def _build_system_prompt(
        self,
        session_key: str = "",
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
        query: str | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Assemble the full system prompt from cached + dynamic layers.

        Cache strategy (three partitioned layers):
          - **Static**: base prompt + skills + tools (rebuilt once).
          - **Memory context**: per (session, query_bucket) — survives across
            exchanges, invalidated on remember/forget.
          - **History summaries**: per session — invalidated after compaction.
          - **Dynamic**: session notes + file context — always fresh.

        A query bucket (hash mod 20) groups similar queries together so
        the LLM relevance-filtering result is reused across exchanges.
        """
        parts: list[str] = []

        # -- Layer 1: Static (base + skills + tools) --------------------------

        static = await self._build_static_prompt(tools, skills)
        if static:
            parts.append(static)

        # -- Layer 2: Memory context (cached per session:query_bucket) --------

        mem_key = (
            f"{session_key or 'default'}:{hash(query or '') % 20}"
        )
        if mem_key not in self._memory_cache:
            self._memory_cache[mem_key] = self._build_memory_context()
            # Evict oldest if at capacity
            if len(self._memory_cache) > self._memory_cache_max:
                oldest = next(iter(self._memory_cache))
                self._memory_cache.pop(oldest)
        memory_ctx = self._memory_cache[mem_key]
        if memory_ctx.strip():
            parts.append(memory_ctx)

        # -- Dynamic (never cached) ------------------------------------------

        # File context recovery
        if messages:
            file_ctx = self._extract_file_context(messages)
            if file_ctx:
                parts.append(file_ctx)

        # Recent history (unprocessed history.jsonl entries since last Dream)
        if session_key:
            history_ctx = self._build_history_context()
            if history_ctx:
                parts.append(history_ctx)

        # Language enforcement: append as the LAST section so recency bias
        # overrides any English tone set by SOUL.md or other upstream content.
        parts.append(
            "# Language Rule\n\n"
            "Your response language MUST match the user's message language. "
            "If the user writes in Chinese, you MUST reply in Chinese. "
            "If the user writes in English, you MUST reply in English. "
            "This rule overrides any language cues in your identity (SOUL.md) "
            "or the user's profile (USER.md)."
        )

        return "\n\n".join(parts)

    def _build_history_context(self, max_entries: int = 20, max_chars: int = 16_000) -> str:
        """Build a 'Recent History' section from unprocessed history.jsonl entries."""
        dream_cursor = self.store.get_dream_cursor()
        entries = self.store.read_history(since_cursor=dream_cursor)
        if not entries:
            return ""
        entries = entries[-max_entries:]
        lines = [f"- {e['content']}" for e in entries]
        text = "# Recent History\n\n" + "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text

    async def _build_static_prompt(
        self,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> str:
        """Build the static portion of the system prompt (base + skills + tools).

        Cached indefinitely — only invalidated when tools are registered or
        unregistered (:meth:`_invalidate_static`).
        """
        if self._static_prompt is not None:
            return self._static_prompt

        parts: list[str] = []

        if self.system_prompt:
            parts.append(self.system_prompt)
        else:
            parts.append(_DEFAULT_SYSTEM_PROMPT)

        autoload_skills = self.skills_loader.build_skills_summary()
        explicit_skills = self.skills_loader.load_skills_for_context(skills or [])
        if not explicit_skills and skills:
            explicit_skills = "\n\n".join(
                f"### Skill: {name}\n\n(no skill file found)" for name in skills
            )
        skills_content = "\n\n".join(
            s for s in (autoload_skills, explicit_skills) if s
        )
        if skills_content:
            parts.append(
                render_template("agent/skills_section.md", skills_summary=skills_content)
            )

        if tools is not None:
            tool_defs = tools.get_definitions()
            if tool_defs:
                tool_lines = "\n".join(
                    f"- **{t['function']['name']}**: {t['function'].get('description', '')}"
                    for t in tool_defs
                )
                parts.append(f"# Available Tools\n\n{tool_lines}")

        self._static_prompt = "\n\n".join(parts) if parts else ""
        return self._static_prompt

    # ========================================================================
    # Rebuild after compaction (fixes stale system prompt — P0 fix)
    # ========================================================================

    async def _rebuild_after_compact(
        self,
        session_key: str,
        current_input: str,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild the message list after compaction.

        Key fix: the system prompt is **rebuilt** to include newly-written
        history.jsonl summaries.  Previously the old ``system_content``
        was reused, causing summaries to be one turn stale.
        """
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor

        # Non-destructive repair + slice from cursor
        raw_history, _ = self._repair_messages(session.messages)
        raw_history = raw_history[cursor:]
        raw_history = raw_history[-self.token_budget.max_history_messages:]

        history = self.compaction.micro_compact(
            raw_history,
            keep_recent_turns=self.token_budget.micro_compact_keep_turns,
            placeholder=self.token_budget.micro_compact_placeholder,
        )
        history = [m for m in history if m.get("role") != "system"]
        history = _truncate_tool_results(
            history, self.token_budget.history_tool_result_max_chars,
        )
        history = _truncate_tool_call_args(
            history, self.token_budget.tool_call_args_max_chars,
        )

        # Rebuild system prompt (includes new history summaries + memory relevance)
        system_content = await self._build_system_prompt(
            session_key, tools=tools, skills=skills, query=current_input,
            messages=history,
        )

        return [
            {"role": "system", "content": system_content},
        ] + history + [
            {"role": "user", "content": current_input},
        ]

    # ========================================================================
    # Interrupt repair
    # ========================================================================

    @staticmethod
    def _repair_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Return *messages* with unmatched pairs repaired."""
        if not messages:
            return messages, 0

        repaired: list[dict[str, Any]] = []
        fixed_count = 0

        tool_results: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                if tcid:
                    tool_results.add(tcid)

        # Pass 1: fix unmatched tool calls
        for msg in messages:
            repaired.append(msg)
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id and tc_id not in tool_results:
                        repaired.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": _INTERRUPT_TOOL_RESULT,
                        })
                        tool_results.add(tc_id)
                        fixed_count += 1

        # Pass 2: check for interrupted trailing message
        if repaired:
            last_role = repaired[-1].get("role")
            if last_role in ("user", "tool"):
                repaired.append({
                    "role": "assistant",
                    "content": _INTERRUPT_MESSAGE,
                    "timestamp": datetime.now().isoformat(),
                })
                fixed_count += 1

        return repaired, fixed_count

    # ========================================================================
    # File context recovery (post-compaction — mitigates P2 #8)
    # ========================================================================

    _FILE_PATH_RE = re.compile(
        r'(?:^|[\s`"''(])'
        r'(/?[\w.-]+(?:/[\w.-]+)+\.\w{1,10}'
        r'|~?/\w+(?:/[\w.-]+)+\.?\w*)'
        r'|@[\w./-]+\.\w+',
    )

    @classmethod
    def _extract_file_context(
        cls, messages: list[dict[str, Any]], *, max_files: int = 5,
    ) -> str:
        """Scan recent user/assistant messages for file-path references.

        After compression, older messages are summarised and their file
        references are lost.  This method recovers recently mentioned file
        paths so the model retains awareness of what files are in play.

        Returns an empty string when no paths are found.
        """
        if not messages:
            return ""

        seen: set[str] = set()
        # Scan from newest to oldest — recent files are more relevant
        for msg in reversed(messages):
            if msg.get("role") not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for m in cls._FILE_PATH_RE.finditer(content):
                path = m.group().strip().lstrip("(`\"'")
                if path and len(path) > 1 and path not in seen:
                    seen.add(path)
                    if len(seen) >= max_files:
                        break
            if len(seen) >= max_files:
                break

        if not seen:
            return ""

        lines = [f"- `{p}`" for p in sorted(seen)]
        return (
            "# Files in Context\n\n"
            "The following files have been mentioned in recent conversation:\n\n"
            + "\n".join(lines)
        )

    # ========================================================================
    # Memory access — direct MEMORY.md operations
    # ========================================================================

    def _build_memory_context(self) -> str:
        """Build the memory section for system-prompt injection.

        Reads SOUL.md, USER.md, and MEMORY.md directly from the store.
        """
        parts: list[str] = []

        soul = self.store.read_soul()
        if soul.strip():
            parts.append(f"# Identity (SOUL.md)\n\n{soul}")

        user = self.store.read_user()
        if user.strip() and not self._is_user_template(user):
            parts.append(f"# User Profile (USER.md)\n\n{user}")

        memory_ctx = self.store.get_memory_context()
        if memory_ctx.strip():
            parts.append(memory_ctx)

        return "\n\n---\n\n".join(parts) if parts else ""

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
    ) -> None:
        """Append a fact to MEMORY.md (dedup by content).

        Hand-written entries and Dream-generated entries coexist in the
        same file; Dream's dedup logic avoids duplicating facts that
        already appear.
        """
        current = self.store.read_memory_file()
        # Dedup: skip if content already appears
        if content.strip().lower() in current.lower():
            return
        entry = f"- [{mem_type}] {name}: {content}"
        if description:
            entry += f"  # {description}"
        updated = current.rstrip() + "\n" + entry + "\n"
        self.store.write_memory_file(updated)
        self._invalidate_memory_cache()

    def forget(self, name: str) -> bool:
        """Remove a fact from MEMORY.md by name match."""
        current = self.store.read_memory_file()
        lines = current.splitlines()
        new_lines: list[str] = []
        removed = False
        for line in lines:
            if f"[{name}]" in line or (line.startswith("- ") and name.lower() in line.lower()):
                removed = True
                continue
            new_lines.append(line)
        if removed:
            self.store.write_memory_file("\n".join(new_lines) + "\n")
            self._invalidate_memory_cache()
            return True
        return False

    def recall(self, query: str, *, top_n: int = 10) -> list[dict]:
        """Keyword search in MEMORY.md content.

        Returns list of dicts with name/content/type keys.
        """
        current = self.store.read_memory_file()
        if not query.strip():
            return []
        query_lower = query.lower()
        results: list[dict] = []
        for line in current.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            if query_lower not in line.lower():
                continue
            # Parse: "- [type] name: content"
            rest = line[2:]  # strip "- "
            entry: dict = {"raw": rest}
            if rest.startswith("[") and "] " in rest:
                bracket_end = rest.index("] ")
                entry["mem_type"] = rest[1:bracket_end]
                rest = rest[bracket_end + 2:]
            # Split name: content
            if ": " in rest:
                name_part, content_part = rest.split(": ", 1)
                entry["name"] = name_part.strip()
                entry["content"] = content_part.split("  #")[0].strip()
            else:
                entry["name"] = rest.strip()
                entry["content"] = rest.strip()
            if "content" in entry:
                results.append(entry)
            if len(results) >= top_n:
                break
        return results

    @staticmethod
    def _is_user_template(content: str) -> bool:
        """Return True if USER.md content looks like an unfilled template."""
        markers = [
            "Edit this file to customize",
            "(your timezone",
            "(your role",
            "(preferred language",
        ]
        lower = content.lower()
        return any(m.lower() in lower for m in markers)

    # ========================================================================
    # Prompt cache
    # ========================================================================

    # ========================================================================
    # Cache invalidation
    # ========================================================================

    def _invalidate_static(self) -> None:
        """Invalidate the static prompt layer (base + skills + tools)."""
        self._static_prompt = None

    def _invalidate_memory_cache(self, session_key: str | None = None) -> None:
        """Invalidate the memory-context cache layer.

        Called on remember() / forget().  If *session_key* is None, clears
        all memory caches.
        """
        if session_key is None:
            self._memory_cache.clear()
        else:
            prefix = f"{session_key}:"
            keys = [k for k in self._memory_cache if k.startswith(prefix)]
            for k in keys:
                self._memory_cache.pop(k, None)
