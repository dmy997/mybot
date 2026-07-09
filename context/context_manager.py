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

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from context.compaction import CompactionService, _estimate_message_tokens
from context.token_budget import TokenBudget
from memory.consolidator import Consolidator
from utils import render_template

from .memory_service import MemoryService
from .session_store import SessionStore

if TYPE_CHECKING:
    from tools import ToolRegistry

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
    "YOU MUST communicate in the same language as the user. "
    "If the user writes in Chinese, ALL your visible responses MUST be in Chinese. "
    "If the user writes in English, ALL your visible responses MUST be in English. "
    "This applies at all times — including during and after tool calls. "
    "SOUL.md and USER.md are secondary; the user's language ALWAYS takes priority."
)

# Session repair constants
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

    _FILE_PATH_RE = re.compile(
        r'(?:^|[\s`"''(])'
        r'(/?[\w.-]+(?:/[\w.-]+)+\.\w{1,10}'
        r'|~?/\w+(?:/[\w.-]+)+\.?\w*)'
        r'|@[\w./-]+\.\w+',
    )

    def __init__(
        self,
        workspace: Path,
        *,
        provider: Any | None = None,
        system_prompt: str = "",
        max_context_tokens: int = 200_000,
        max_output_tokens: int = 20_000,
        warning_buffer_ratio: float = 0.11,
        auto_compact_buffer_ratio: float = 0.072,
        block_buffer_ratio: float = 0.017,
        idle_compress_seconds: int = 300,
        compress_model: str | None = None,
        compress_ratio: float = 0.5,
        consolidation_ratio: float = 0.7,
        disabled_skills: list[str] | None = None,
        hybrid_store: object | None = None,
        max_session_messages: int = 2000,
        session_ttl_days: int = 30,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.system_prompt = system_prompt

        from core.skills import SkillsLoader as _SkillsLoader

        # Session persistence (composition)
        self.session_store = SessionStore(
            self.workspace,
            max_session_messages=max_session_messages,
            session_ttl_days=session_ttl_days,
        )
        self.session = self.session_store.session  # backward-compat alias

        # Memory operations (composition)
        self.memory = MemoryService(self.workspace, hybrid_store=hybrid_store)
        self.store = self.memory.store  # backward-compat alias

        self.skills_loader = _SkillsLoader(self.workspace, disabled_skills=disabled_skills)

        # Consolidator — per-turn LLM summarization → history.jsonl
        self.consolidator = Consolidator(
            store=self.store,
            provider=provider,
            model=compress_model or "",
            context_window_tokens=max_context_tokens,
            consolidation_ratio=consolidation_ratio,
        )

        # Unified token budget
        self.token_budget = TokenBudget(
            context_window=max_context_tokens,
            max_output_tokens=max_output_tokens,
            warning_buffer_ratio=warning_buffer_ratio,
            auto_compact_buffer_ratio=auto_compact_buffer_ratio,
            block_buffer_ratio=block_buffer_ratio,
            compress_ratio=compress_ratio,
            idle_compress_seconds=idle_compress_seconds,
        )

        # Unified compaction service (cursor advancement, no LLM)
        self.compaction = CompactionService(
            token_budget=self.token_budget,
            session_manager=self.session,
        )

        # Three-layer partitioned prompt cache
        self._static_prompt: str | None = None
        self._memory_cache: dict[str, str] = {}
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
    # Delegation — session persistence (→ SessionStore)
    # ========================================================================

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
        await self.session_store.save_exchange(
            session_key, user_input, assistant_messages,
            tools_used=tools_used, errors=errors,
        )

    async def save_session(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist the full message list after an agent run."""
        await self.session_store.save_session(session_key, messages)

    def get_history(self, session_key: str) -> list[dict[str, Any]]:
        """Return session messages without the system prompt."""
        return self.session_store.get_history(session_key)

    def delete_session(self, session_key: str) -> bool:
        """Delete a session from disk and memory."""
        return self.session_store.delete_session(session_key)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.session_store.list_sessions()

    def purge_expired_sessions(self) -> int:
        """Delete sessions whose last update exceeds ``session_ttl_days``."""
        return self.session_store.purge_expired_sessions()

    # ========================================================================
    # Delegation — memory operations (→ MemoryService)
    # ========================================================================

    def _build_memory_context(self) -> str:
        """Build the memory section for system-prompt injection."""
        return self.memory.build_memory_context()

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
    ) -> None:
        """Append a fact to MEMORY.md (dedup by content)."""
        self.memory.remember(name, content, mem_type=mem_type, description=description)
        self._invalidate_memory_cache()

    def forget(self, name: str) -> bool:
        """Remove a fact from MEMORY.md by name match."""
        result = self.memory.forget(name)
        if result:
            self._invalidate_memory_cache()
        return result

    def recall(
        self, query: str, *, top_n: int = 10, session_key: str | None = None,
    ) -> list[dict]:
        """Search memory content. Uses hybrid search when available."""
        return self.memory.recall(query, top_n=top_n, session_key=session_key)

    # ========================================================================
    # System prompt assembly (inlined from PromptBuilderMixin)
    # ========================================================================

    async def _build_system_prompt(
        self,
        session_key: str = "",
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
        query: str | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Assemble the full system prompt from cached + dynamic layers."""
        parts: list[str] = []

        static = await self._build_static_prompt(tools, skills)
        if static:
            parts.append(static)

        mem_key = f"{session_key or 'default'}:{hash(query or '') % 20}"
        if mem_key not in self._memory_cache:
            self._memory_cache[mem_key] = self._build_memory_context()
            if len(self._memory_cache) > self._memory_cache_max:
                oldest = next(iter(self._memory_cache))
                self._memory_cache.pop(oldest)
        memory_ctx = self._memory_cache[mem_key]
        if memory_ctx.strip():
            parts.append(memory_ctx)

        if messages:
            file_ctx = self._extract_file_context(messages)
            if file_ctx:
                parts.append(file_ctx)

        if session_key:
            history_ctx = self._build_history_context()
            if history_ctx:
                parts.append(history_ctx)

        parts.append(
            "# LANGUAGE RULE — HIGHEST PRIORITY\n\n"
            "**This rule overrides everything else in this system prompt.**\n\n"
            "Your visible responses MUST be in the same language as the user's message. "
            "If the user writes in Chinese, you MUST reply in Chinese. "
            "If the user writes in English, you MUST reply in English.\n\n"
            "- Tool calls and tool results do NOT change this rule. "
            "After executing tools, continue responding in the user's language.\n"
            "- Code blocks, file paths, and technical terms are the only exception "
            "— they may appear in English as needed.\n"
            "- This rule overrides SOUL.md identity, USER.md profile, or any "
            "English content in tool outputs."
        )

        return "\n\n".join(parts)

    def _build_history_context(
        self, max_entries: int = 20, max_chars: int = 16_000,
    ) -> str:
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
        """Build the static portion of the system prompt (base + skills + tools)."""
        if self._static_prompt is not None:
            return self._static_prompt

        parts: list[str] = []

        if self.system_prompt:
            parts.append(self.system_prompt)
        else:
            parts.append(_DEFAULT_SYSTEM_PROMPT)

        autoload_skills = self.skills_loader.build_skills_summary()
        explicit_skills = self.skills_loader.load_skills_for_context(
            skills or [],
        )
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

        result = "\n\n".join(parts) if parts else ""
        self._static_prompt = result
        return result

    async def _rebuild_after_compact(
        self,
        session_key: str,
        current_input: str,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild the message list after compaction."""
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor

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

        system_content = await self._build_system_prompt(
            session_key, tools=tools, skills=skills, query=current_input,
            messages=history,
        )

        return [
            {"role": "system", "content": system_content},
        ] + history + [
            {"role": "user", "content": current_input},
        ]

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

    @classmethod
    def _extract_file_context(
        cls, messages: list[dict[str, Any]], *, max_files: int = 5,
    ) -> str:
        """Scan recent user/assistant messages for file-path references."""
        if not messages:
            return ""

        seen: set[str] = set()
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

    def _invalidate_static(self) -> None:
        """Invalidate the static prompt layer (base + skills + tools)."""
        self._static_prompt = None

    def _invalidate_memory_cache(self, session_key: str | None = None) -> None:
        """Invalidate the memory-context cache layer."""
        if session_key is None:
            self._memory_cache.clear()
        else:
            prefix = f"{session_key}:"
            keys = [k for k in self._memory_cache if k.startswith(prefix)]
            for k in keys:
                self._memory_cache.pop(k, None)

    # ========================================================================
    # Core context assembly (inlined from CoreContextMixin)
    # ========================================================================

    async def build_messages(
        self,
        session_key: str,
        current_input: str,
        *,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Assemble the complete message list for an agent run."""
        budget = self.token_budget
        if context_window is not None or max_output_tokens is not None:
            budget = TokenBudget(
                context_window=context_window or budget.context_window,
                max_output_tokens=max_output_tokens or budget.max_output_tokens,
                warning_buffer_ratio=budget.warning_buffer_ratio,
                auto_compact_buffer_ratio=budget.auto_compact_buffer_ratio,
                block_buffer_ratio=budget.block_buffer_ratio,
                compress_ratio=budget.compress_ratio,
                idle_compress_seconds=budget.idle_compress_seconds,
            )
        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor

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

        system_content = await self._build_system_prompt(
            session_key, tools=tools, skills=skills, query=current_input,
            messages=history,
        )

        lang_hint = _language_hint(current_input)

        preliminary: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ] + history + ([
            {"role": "system", "content": lang_hint},
        ] if lang_hint else []) + [
            {"role": "user", "content": current_input},
        ]

        if budget.context_window <= 0:
            return preliminary

        total = _estimate_message_tokens(preliminary)

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

        elif total > budget.warning_threshold:
            logger.warning(
                "Context at {} tokens ({}% of window), consider compacting",
                total, total / budget.effective_window * 100,
            )

        return preliminary

    async def compress(
        self,
        session_key: str,
        *,
        keep_recent: int | None = None,
        budget_tokens: int | None = None,
    ) -> int:
        """Compress unsummarised messages (delegates to :class:`CompactionService`)."""
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
        """User-triggered full compaction with LLM summarisation."""
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


# =============================================================================
# Module-level helpers (used by ContextManager methods)
# =============================================================================


def _language_hint(text: str) -> str | None:
    """Return a language enforcement hint if the user writes in a non-English language."""
    if re.search(r'[一-鿿]', text):
        return (
            "IMPORTANT LANGUAGE REMINDER: The user's message above is in "
            "Chinese. You MUST reply entirely in Chinese. This applies "
            "throughout your entire response — including before, during, "
            "and after any tool calls. Do not mix languages."
        )
    return None


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
