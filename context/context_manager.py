"""ContextManager — unified context assembly, compression, and session persistence.

Owns the full lifecycle of "what the LLM sees":
1. Interrupted-session repair (unmatched tool calls, missing assistant responses)
2. System prompt assembly (base + memory + skills + tools + history summaries)
3. Unified compression (idle and token-budget share the same method)
4. Session history loading (cursor-based, 100-message cap, turn-boundary-safe)
5. Token-budget check with pre-emptive compression
6. Memory read (context injection) and write (persisting exchange excerpts)
"""

from __future__ import annotations

import json as _json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    pass

from memory import MemoryManager
from memory.store import MemoryStore
from tools import ToolRegistry
from utils import render_template

from .session import SessionManager

# ---------------------------------------------------------------------------
# Token estimation (lightweight, no network call)
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
    def _count_tokens(text: str) -> int:
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


def _estimate_message_chars(messages: list[dict[str, Any]]) -> int:
    """Character count for a list of chat messages (fallback metric)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part))
        if "tool_calls" in msg:
            total += len(str(msg["tool_calls"]))
    return total


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_CONTEXT_TOKENS = 200_000
_DEFAULT_IDLE_COMPRESS_SECONDS = 300
_COMPRESS_RECENT_RATIO = 0.5
_MAX_HISTORY_MESSAGES = 100
_SUMMARY_MAX_WORDS = 200
_CONTENT_TRUNCATE_LENGTH = 2000
_DEHYDRATE_MAX_CONTENT_CHARS = 3000
_INTERRUPT_MESSAGE = "Error: Task interrupted before a response was generated."
_INTERRUPT_TOOL_RESULT = "Error: Tool execution interrupted."

# Patterns for dehydration
_DATA_URI_RE = re.compile(r"data:[^;\"\s]*;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|bmp|ico)(\s|$)", re.IGNORECASE)


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
        When ``None``, compression falls back to simple truncation.
    system_prompt:
        Base system prompt prepended to every request.
    max_context_tokens:
        Soft token budget. When exceeded, unsummarised messages are compressed.
    idle_compress_seconds:
        When a session has been idle longer than this, summarise older
        messages on the next access.  Set to ``0`` to disable.
    compress_model:
        Optional model override for compression calls (defaults to provider
        default — set this to a cheap model like ``"gpt-4o-mini"``).
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
        max_context_tokens: int = _DEFAULT_MAX_CONTEXT_TOKENS,
        idle_compress_seconds: int = _DEFAULT_IDLE_COMPRESS_SECONDS,
        compress_model: str | None = None,
        compress_ratio: float = _COMPRESS_RECENT_RATIO,
        disabled_skills: list[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.provider = provider
        self.system_prompt = system_prompt
        self.max_context_tokens = max_context_tokens
        self.idle_compress_seconds = idle_compress_seconds
        self.compress_model = compress_model
        self.compress_ratio = compress_ratio

        from core.skills import SkillsLoader as _SkillsLoader

        self.session = SessionManager(self.workspace)
        self.memory = MemoryManager(MemoryStore(self.workspace))
        self.skills_loader = _SkillsLoader(self.workspace, disabled_skills=disabled_skills)

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
        2. System prompt (base + memory + skills + tools + history summaries)
        3. Session history from ``consolidated_cursor`` onwards (max 100)
        4. Current user input
        5. Token-budget check → compress if over budget → rebuild

        Returns a list ready for ``AgentInput.init_messages``.
        """
        self._repair_session(session_key)

        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor

        # 1. System prompt (includes history.jsonl summaries)
        system_content = self._build_system_prompt(
            session_key, tools=tools, skills=skills,
        )

        # 2. Load unsummarised history (cursor → end, capped at 100)
        raw_history = session.messages[cursor:]
        raw_history = raw_history[-_MAX_HISTORY_MESSAGES:]

        # 3. Filter out system messages from history
        history: list[dict[str, Any]] = [
            m for m in raw_history if m.get("role") != "system"
        ]

        # 4. Assemble preliminary list and check budget
        preliminary: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ] + history + [
            {"role": "user", "content": current_input},
        ]

        if self.max_context_tokens > 0:
            total = _estimate_message_tokens(preliminary)
            if total > self.max_context_tokens:
                # Compress unsummarised messages to free budget
                sys_tokens = _estimate_message_tokens([preliminary[0]])
                user_tokens = _estimate_message_tokens([preliminary[-1]])
                history_budget = (
                    int(self.max_context_tokens * self.compress_ratio)
                    - sys_tokens - user_tokens
                )
                if history_budget > 0:
                    await self.compress(session_key, budget_tokens=history_budget)

                # Rebuild after compression
                session = self.session.get_session(session_key)
                cursor = session.consolidated_cursor
                raw_history = session.messages[cursor:]
                raw_history = raw_history[-_MAX_HISTORY_MESSAGES:]
                history = [m for m in raw_history if m.get("role") != "system"]

                preliminary = [
                    {"role": "system", "content": system_content},
                ] + history + [
                    {"role": "user", "content": current_input},
                ]

        return preliminary

    # -- unified compression --------------------------------------------------

    async def compress(
        self,
        session_key: str,
        *,
        keep_recent: int | None = None,
        budget_tokens: int | None = None,
    ) -> int:
        """Compress unsummarised messages (those after ``consolidated_cursor``).

        Exactly one of *keep_recent* or *budget_tokens* must be provided:

        - **keep_recent**: keep this many most-recent messages (idle compression)
        - **budget_tokens**: keep as many recent messages as fit within this
          token budget (token-budget compression)

        The split point is adjusted to preserve user/assistant turn boundaries.

        Original ``session.messages`` are **never modified** — only
        ``consolidated_cursor`` is advanced and the summary is appended to
        ``history.jsonl``.

        Returns the number of messages compressed (0 if nothing was done).
        """
        if keep_recent is None and budget_tokens is None:
            raise ValueError("One of keep_recent or budget_tokens is required")
        if keep_recent is not None and budget_tokens is not None:
            raise ValueError("Only one of keep_recent or budget_tokens allowed")

        if self.idle_compress_seconds <= 0 and budget_tokens is not None:
            pass  # budget compression is always allowed
        elif self.idle_compress_seconds <= 0:
            return 0

        session = self.session.get_session(session_key)
        cursor = session.consolidated_cursor
        unsummarised = session.messages[cursor:]

        if len(unsummarised) <= 1:
            return 0

        # Determine how many recent messages to keep
        if keep_recent is not None:
            keep_count = keep_recent
        else:
            keep_count = self._fit_in_budget(unsummarised, budget_tokens)

        if keep_count >= len(unsummarised):
            return 0

        to_compress = list(unsummarised[:-keep_count])
        to_keep = list(unsummarised[-keep_count:])

        # Adjust split to preserve turn boundaries
        self._adjust_split(to_compress, to_keep)

        if not to_compress:
            return 0

        # Dehydrate + summarise
        dehydrated = self._dehydrate_messages(to_compress)
        summary = await self._summarise(dehydrated)

        # Persist summary to history.jsonl
        self._write_history(session_key, len(to_compress), summary)

        # Advance cursor (session.messages is NOT modified)
        session.consolidated_cursor = cursor + len(to_compress)
        session.updated_at = datetime.now()
        self.session.save_session(session)

        logger.debug(
            "Compression for {!r}: {} messages summarised, cursor {} → {}",
            session_key,
            len(to_compress),
            cursor,
            session.consolidated_cursor,
        )
        return len(to_compress)

    # -- session lifecycle ----------------------------------------------------

    def save_exchange(
        self,
        session_key: str,
        user_input: str,
        assistant_messages: list[dict[str, Any]],
    ) -> None:
        """Append a user+assistant exchange to the session log.

        Unlike :meth:`save_session` which replaces the entire message list,
        this appends only the new exchange, preserving the raw conversation
        log and keeping ``consolidated_cursor`` meaningful.
        """
        session = self.session.get_session(session_key)
        session.messages.append({"role": "user", "content": user_input})
        for msg in assistant_messages:
            session.messages.append(msg)
        session.updated_at = datetime.now()
        self.session.save_session(session)

    def save_session(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist the full message list after an agent run.

        Prefer :meth:`save_exchange` for normal use — it preserves the
        raw message log and cursor integrity.  Use this method only when
        you need to replace the entire session.
        """
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
    # System prompt assembly
    # ========================================================================

    def _build_system_prompt(
        self,
        session_key: str = "",
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> str:
        """Assemble the full system prompt from all configured sources."""
        parts: list[str] = []

        # 0. Base system prompt
        if self.system_prompt:
            parts.append(self.system_prompt)

        # 1. Memory context (SOUL.md, USER.md, long-term memory)
        memory_ctx = self.memory.build_memory_context()
        if memory_ctx.strip():
            parts.append(memory_ctx)

        # 2. History summaries from previous compressions
        if session_key:
            history_ctx = self._read_history_summaries(session_key)
            if history_ctx:
                parts.append(history_ctx)

        # 3. Skills
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
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_content))

        # 4. Tools
        if tools is not None:
            tool_defs = tools.get_definitions()
            if tool_defs:
                tool_lines = "\n".join(
                    f"- **{t['function']['name']}**: {t['function'].get('description', '')}"
                    for t in tool_defs
                )
                parts.append(f"# Available Tools\n\n{tool_lines}")

        return "\n\n".join(parts)

    # ========================================================================
    # Interrupt repair
    # ========================================================================

    def _repair_session(self, session_key: str) -> None:
        """Check and repair unmatched message pairs caused by interrupts.

        Detects three interruption patterns:
        1. Assistant message with ``tool_calls`` but no matching tool results
        2. Last message is a ``user`` message with no assistant response
        3. Last message is a ``tool`` result with no assistant response
        """
        session = self.session.get_session(session_key)
        if not session.messages:
            return

        repaired, fixed_count = self._repair_messages(session.messages)
        if fixed_count > 0:
            session.messages = repaired
            session.updated_at = datetime.now()
            self.session.save_session(session)
            logger.debug(
                "Session {!r} repaired: {} unmatched pairs fixed",
                session_key, fixed_count,
            )

    @staticmethod
    def _repair_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Return *messages* with unmatched pairs repaired.

        Returns ``(repaired_messages, fixed_count)``.
        """
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
    # History summaries (read/write)
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

    def _read_history_summaries(self, session_key: str) -> str:
        """Read history.jsonl and format summaries for the system prompt."""
        path = self._history_path(session_key)
        if not path.exists():
            return ""

        parts: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                record = _json.loads(line)
                ts = record.get("timestamp", "")[:19]
                summary = record.get("summary", "")
                compressed = record.get("compressed_count", 0)
                if summary:
                    parts.append(
                        f"## Historical Summary ({ts}, {compressed} messages)\n\n{summary}"
                    )
        except (_json.JSONDecodeError, OSError):
            return ""

        if not parts:
            return ""

        return (
            "# Previous Conversation Summaries\n\n"
            "The following summaries capture earlier parts of this conversation "
            "that have been archived:\n\n" + "\n\n".join(parts)
        )

    # ========================================================================
    # Compression helpers
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
        # Ensure at least 1 message is kept
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
                # Need the preceding assistant (with tool_calls)
                prev = to_compress[-1]
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    to_keep.insert(0, to_compress.pop())
                else:
                    break
            elif role == "assistant" and not first_keep.get("tool_calls"):
                # Need the preceding user message
                prev = to_compress[-1]
                if prev.get("role") == "user":
                    to_keep.insert(0, to_compress.pop())
                else:
                    break
            else:
                # "user" or "assistant" with tool_calls → safe
                break

    @staticmethod
    def _dehydrate_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Strip non-critical payload before sending to the summarisation API.

        - Truncates content strings to ``_DEHYDRATE_MAX_CONTENT_CHARS``
        - Replaces base64 data URIs with ``[binary: ...]`` placeholders
        - Drops tool call arguments (keeps function names only)
        """
        dehydrated: list[dict[str, Any]] = []
        for msg in messages:
            d: dict[str, Any] = {}
            for k, v in msg.items():
                if k == "content" and isinstance(v, str):
                    # Strip data URIs
                    v = _DATA_URI_RE.sub("[binary data removed]", v)
                    # Truncate oversized content
                    if len(v) > _DEHYDRATE_MAX_CONTENT_CHARS:
                        v = v[:_DEHYDRATE_MAX_CONTENT_CHARS] + (
                            f"\n[... {len(v) - _DEHYDRATE_MAX_CONTENT_CHARS} "
                            f"more chars truncated]"
                        )
                    d[k] = v
                elif k == "tool_calls" and isinstance(v, list):
                    # Keep only function names to prevent OOM from huge arguments
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

    # ========================================================================
    # Summarisation (shared by compress → idle and budget paths)
    # ========================================================================

    async def _summarise(self, messages: list[dict[str, Any]]) -> str:
        """Summarise *messages* via LLM, falling back to hard truncation."""
        if self.provider is None:
            return self._truncate_summary(messages)
        try:
            return await self._llm_summarise(messages)
        except Exception:
            logger.opt(exception=True).warning(
                "LLM summarisation failed, falling back to truncation"
            )
            return self._truncate_summary(messages)

    @staticmethod
    def _truncate_summary(
        messages: list[dict[str, Any]], *, max_chars: int = 2000,
    ) -> str:
        """Hard-truncation fallback: concatenate truncated messages.

        Each message is capped at 150 chars; the total summary is capped at
        *max_chars*.  This is a last-resort fallback when the LLM summarisation
        API is unavailable.
        """
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
            # First/last message keep more detail; middle messages get shorter
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

    async def _llm_summarise(self, messages: list[dict[str, Any]]) -> str:
        """Use a lightweight LLM call to summarise messages.

        Messages should already be dehydrated before calling this method.
        """
        slim: list[dict[str, Any]] = [
            {**m, "content": (
                m.get("content", "")[:_CONTENT_TRUNCATE_LENGTH] + "..."
                if isinstance(m.get("content", ""), str)
                and len(m.get("content", "")) > _CONTENT_TRUNCATE_LENGTH
                else m.get("content", "")
            )}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]

        if not slim:
            return "(empty context)"

        summary_prompt = render_template(
            "context/summary.md", max_words=_SUMMARY_MAX_WORDS, strip=True,
        )
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
    # Memory access (long-term user/feedback/project/reference)
    # ========================================================================

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
    ) -> None:
        """Create or update a long-term memory entry."""
        self.memory.remember(name, content, mem_type=mem_type, description=description)

    def forget(self, name: str) -> bool:
        """Delete a long-term memory entry."""
        return self.memory.forget(name)

    def recall(self, query: str, *, top_n: int = 10) -> list[Any]:
        """Search long-term memories by keyword."""
        return self.memory.recall(query, top_n=top_n)
