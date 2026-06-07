"""ContextManager — unified context assembly, compression, and session persistence.

Owns the full lifecycle of "what the LLM sees":
1. Interrupted-session repair (unmatched tool calls, missing assistant responses)
2. Idle compression (auto-summarise stale conversations)
3. System prompt assembly (base prompt + memory + tools + skills)
4. Session history loading / saving
5. Token-budget compression (on-the-fly when context is too long)
6. Memory read (context injection) and write (persisting exchange summaries)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.skills import SkillsLoader

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


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_CONTEXT_TOKENS = 128_000
_DEFAULT_IDLE_COMPRESS_SECONDS = 300
_COMPRESS_RECENT_RATIO = 0.7
_IDLE_KEEP_RECENT = 4
_SUMMARY_PROMPT = (
    "Summarise the conversation above. Keep all key facts, decisions, "
    "and action items. Use no more than {max_words} words."
)
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
        When ``None``, compression falls back to simple truncation.
    system_prompt:
        Base system prompt prepended to every request.
    max_context_tokens:
        Soft token budget. When exceeded, older messages are compressed.
    idle_compress_seconds:
        When a session has been idle longer than this, summarise older
        messages on the next access.  Set to ``0`` to disable.
    compress_model:
        Optional model override for compression calls (defaults to provider
        default — set this to a cheap model like ``"gpt-4o-mini"``).
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
        disabled_skills: list[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.provider = provider
        self.system_prompt = system_prompt
        self.max_context_tokens = max_context_tokens
        self.idle_compress_seconds = idle_compress_seconds
        self.compress_model = compress_model

        from core.skills import SkillsLoader as _SkillsLoader

        self.session = SessionManager(self.workspace)
        self.memory = MemoryManager(MemoryStore(self.workspace))
        self.skills_loader = _SkillsLoader(self.workspace, disabled_skills=disabled_skills)
    # -- message assembly -----------------------------------------------------

    def build_messages(
        self,
        session_key: str,
        current_input: str,
        *,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Assemble the complete message list for an agent run.

        Composition order:
        1. Repair interrupted session (unmatched tool calls / user messages)
        2. Idle compression (summarise if session is stale)
        3. Fresh system prompt (base + memory + tools + skills)
        4. Session history (without old system messages)
        5. Current user input
        6. Token-budget compression (if over limit)

        Returns a list ready for ``AgentInput.init_messages``.
        """
        # 0. Repair and idle-compress before assembling
        self._repair_session(session_key)
        self._maybe_idle_compress(session_key)

        messages: list[dict[str, Any]] = []

        # 1. System prompt
        system_content = self._build_system_prompt(tools=tools, skills=skills)
        messages.append({"role": "system", "content": system_content})

        # 2. Session history (strip old system messages)
        history = self.session.get_session_history(session_key)
        for msg in history:
            if msg.get("role") != "system":
                messages.append(msg)

        messages.append({"role": "user", "content": current_input})

        # 4. Token-budget compression
        messages = self._maybe_compress(messages, session_key)

        return messages

    # -- system prompt --------------------------------------------------------

    def _build_system_prompt(
        self,
        tools: ToolRegistry | None = None,
        skills: list[str] | None = None,
    ) -> str:
        """Assemble the full system prompt from all configured sources."""
        parts: list[str] = []

        # 0. Base system prompt
        if self.system_prompt:
            parts.append(self.system_prompt)

        # 1. Memory context
        memory_ctx = self.memory.build_memory_context()
        if memory_ctx.strip():
            parts.append(memory_ctx)

        # 2. Skills (via skills_loader + explicit skills)
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

        # 3. Tools
        if tools is not None:
            tool_defs = tools.get_definitions()
            if tool_defs:
                tool_lines = "\n".join(
                    f"- **{t['function']['name']}**: {t['function'].get('description', '')}"
                    for t in tool_defs
                )
                parts.append(f"# Available Tools\n\n{tool_lines}")

        return "\n\n".join(parts)

    # -- interrupt repair -----------------------------------------------------

    def _repair_session(self, session_key: str) -> None:
        """Check and repair unmatched message pairs caused by interrupts.

        Detects two interruption patterns:
        1. Assistant message with ``tool_calls`` but no matching tool results
           → insert synthetic error tool results.
        2. Last message is a ``user`` message with no assistant response
           → append a synthetic error assistant message.
        """
        session = self.session.get_session(session_key)
        if not session.messages:
            return

        repaired, modified = self._repair_messages(session.messages)
        if modified:
            session.messages = repaired
            session.updated_at = datetime.now()
            self.session.save_session(session)
            logger.debug(
                "Session {!r} repaired: {} unmatched pairs fixed",
                session_key,
                len(repaired) - len(session.messages) if modified else 0,
            )

    @staticmethod
    def _repair_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return *messages* with unmatched pairs repaired.

        Returns ``(repaired_messages, was_modified)``.
        """
        if not messages:
            return messages, False

        repaired: list[dict[str, Any]] = []
        modified = False

        # Collect all existing tool_result ids
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
                        modified = True

        # Pass 2: fix unmatched last user message
        if repaired and repaired[-1].get("role") == "user":
            repaired.append({
                "role": "assistant",
                "content": _INTERRUPT_MESSAGE,
                "timestamp": datetime.now().isoformat(),
            })
            modified = True

        return repaired, modified

    # -- idle compression -----------------------------------------------------

    def _maybe_idle_compress(self, session_key: str) -> None:
        """Summarise older messages when *session_key* has been idle."""
        if self.idle_compress_seconds <= 0:
            return

        session = self.session.get_session(session_key)
        if not session.messages:
            return

        elapsed = (datetime.now() - session.updated_at).total_seconds()
        if elapsed <= self.idle_compress_seconds:
            return

        if len(session.messages) <= _IDLE_KEEP_RECENT:
            return

        cutoff = len(session.messages) - _IDLE_KEEP_RECENT
        if cutoff <= session.consolidated_cursor:
            return

        to_summarise = session.messages[:cutoff]
        to_keep = session.messages[cutoff:]

        summary = self._idle_summarise(to_summarise)

        session.messages = [
            {"role": "user", "content": f"[Session summary]\n{summary}"}
        ] + to_keep
        session.consolidated_cursor = cutoff
        session.updated_at = datetime.now()
        self.session.save_session(session)

        logger.debug(
            "Idle compression for {!r}: {} messages → summary, {} kept",
            session_key,
            len(to_summarise),
            len(to_keep),
        )

    def _idle_summarise(self, messages: list[dict[str, Any]]) -> str:
        """Summarise *messages* using the provider, falling back to truncation."""
        if self.provider is None:
            return self._idle_truncate_summary(messages)
        try:
            return self._idle_llm_summarise(messages)
        except Exception:
            logger.opt(exception=True).warning(
                "Idle compression summarisation failed, falling back to truncation"
            )
            return self._idle_truncate_summary(messages)

    def _idle_llm_summarise(self, messages: list[dict[str, Any]]) -> str:
        """Use a lightweight LLM call to summarise messages."""
        slim: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                content = content[:2000] + "..."
            slim.append({**m, "content": content})

        if not slim:
            return "(empty context)"

        slim.append({
            "role": "user",
            "content": _SUMMARY_PROMPT.format(max_words=200),
        })

        import asyncio

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.provider.chat(
                            messages=slim,
                            tools=[],
                            model=self.compress_model,
                            max_tokens=300,
                            temperature=0.0,
                        ),
                    )
                    response = future.result(timeout=30)
            else:
                response = asyncio.run(
                    self.provider.chat(
                        messages=slim,
                        tools=[],
                        model=self.compress_model,
                        max_tokens=300,
                        temperature=0.0,
                    ),
                )
            return response.content or "(summarisation produced no output)"
        except Exception:
            raise

    @staticmethod
    def _idle_truncate_summary(messages: list[dict[str, Any]]) -> str:
        """Simple truncation fallback — first and last meaningful messages."""
        if not messages:
            return "(empty context)"

        parts: list[str] = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    parts.append(f"Conversation started with: {content[:300]}")
                    break

        parts.append(f"(... {len(messages)} messages compressed ...)")

        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    parts.append(f"Last response was: {content[:300]}")
                    break

        return "\n".join(parts)

    # -- token-budget compression ---------------------------------------------

    def _maybe_compress(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> list[dict[str, Any]]:
        """Apply compression if *messages* exceed the token budget."""
        if self.max_context_tokens <= 0:
            return messages

        budget = self.max_context_tokens
        total = _estimate_message_tokens(messages)
        if total <= budget:
            return messages

        logger.info(
            "Context over budget ({} > {} tokens), compressing session {!r}",
            total, budget, session_key,
        )

        system_msg = messages[0]
        rest = messages[1:]
        recent_budget = int(budget * _COMPRESS_RECENT_RATIO)

        recent: list[dict[str, Any]] = []
        recent_tokens = 0
        for msg in reversed(rest):
            t = _estimate_message_tokens([msg])
            if recent_tokens + t > recent_budget:
                break
            recent.insert(0, msg)
            recent_tokens += t

        older = rest[: len(rest) - len(recent)]
        if not older:
            return messages

        summary = self._summarise(older)
        result: list[dict[str, Any]] = [system_msg]
        result.append({
            "role": "user",
            "content": f"[Context summary of earlier conversation]\n\n{summary}",
        })
        result.extend(recent)

        logger.debug(
            "Compressed {} older messages into {} tokens of summary, "
            "keeping {} recent messages ({} tokens)",
            len(older), _count_tokens(summary), len(recent), recent_tokens,
        )

        return result

    def _summarise(self, messages: list[dict[str, Any]]) -> str:
        """Summarise *messages* for token-budget compression."""
        if self.provider is None:
            return self._truncate_summary(messages)
        try:
            return self._llm_summarise(messages)
        except Exception:
            logger.opt(exception=True).warning(
                "LLM summarisation failed, falling back to truncation"
            )
            return self._truncate_summary(messages)

    def _truncate_summary(self, messages: list[dict[str, Any]]) -> str:
        """Simple truncation-based summary."""
        if not messages:
            return "(empty context)"

        parts: list[str] = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    parts.append(f"Conversation started with: {content[:300]}")
                    break

        parts.append(f"(... {len(messages)} messages compressed ...)")

        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    parts.append(f"Last response was: {content[:300]}")
                    break

        return "\n".join(parts)

    def _llm_summarise(self, messages: list[dict[str, Any]]) -> str:
        """Use a lightweight LLM call to summarise older messages."""
        import asyncio

        slim: list[dict[str, Any]] = [
            m for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        if not slim:
            return "(empty context)"

        for msg in slim:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                msg["content"] = content[:2000] + "..."

        max_words = 200
        slim.append({
            "role": "user",
            "content": _SUMMARY_PROMPT.format(max_words=max_words),
        })

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.provider.chat(
                            messages=slim,
                            tools=[],
                            model=self.compress_model,
                            max_tokens=300,
                            temperature=0.0,
                        ),
                    )
                    response = future.result(timeout=30)
            else:
                response = asyncio.run(
                    self.provider.chat(
                        messages=slim,
                        tools=[],
                        model=self.compress_model,
                        max_tokens=300,
                        temperature=0.0,
                    ),
                )
            return response.content or "(summarisation produced no output)"
        except Exception:
            raise

    # -- session lifecycle ----------------------------------------------------

    def save_session(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist the full message list after an agent run."""
        self.session.set_messages(session_key, messages)
        self._record_to_memory(session_key, messages)

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

    # -- memory integration ---------------------------------------------------

    def _record_to_memory(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Record a summary of the latest exchange to long-term memory."""
        last_user = ""
        last_assistant = ""
        for msg in reversed(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if role == "assistant" and not last_assistant:
                last_assistant = str(content)[:500]
            elif role == "user" and not last_user:
                last_user = str(content)[:500]
            if last_user and last_assistant:
                break

        if last_user:
            entry = f"[{session_key}] User: {last_user}"
            if last_assistant:
                entry += f"\n[{session_key}] Assistant: {last_assistant}"
            self.memory.record(entry)

    # -- memory access --------------------------------------------------------

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
