"""Orchestrator — top-level coordination layer.

Wires ContextManager, Dispatcher, and Agents together. Handles:
- Request lifecycle (build context → route → execute → persist)
- Continuous interactive loop (:meth:`run`)
- Crash recovery (Ctrl+C)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

try:
    import readline  # noqa: F401 — enables arrow keys, backspace, history in input()
except ImportError:
    pass
from pathlib import Path
from typing import Any

from loguru import logger

from context.context_manager import ContextManager
from tools import ToolRegistry
from tools.tool import Tool

from .dispatcher import Dispatcher
from .runner import AgentInput

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorResult:
    """Return value for ``Orchestrator._process_once()``."""

    content: str
    session_key: str
    paradigm: str
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Top-level coordinator for the agent framework.

    Parameters
    ----------
    workspace:
        Root directory for sessions, memory, and the recovery journal.
    provider:
        LLM provider shared across context compression and agent execution.
    max_context_tokens:
        Soft token budget forwarded to :class:`ContextManager`.
    idle_compress_seconds:
        When a session has been idle longer than this, summarise older
        messages on the next access.  Set to ``0`` to disable.
    compress_model:
        Optional cheap model override for compression calls.
    dispatcher:
        Pre-built :class:`Dispatcher`.  When provided, auto-discovery is
        skipped and this dispatcher is used directly.
    """

    def __init__(
        self,
        workspace: str | Path,
        provider: Any,  # LLMProvider (lazy import)
        *,
        max_context_tokens: int = 128_000,
        idle_compress_seconds: int = 300,
        compress_model: str | None = None,
        dispatcher: Dispatcher | None = None,
        disabled_skills: list[str] | None = None,
    ) -> None:
        self._running = False
        self.workspace = Path(workspace).expanduser().resolve()

        # Context (idle compression is handled by ContextManager)
        self.ctx = ContextManager(
            self.workspace,
            provider=provider,
            max_context_tokens=max_context_tokens,
            idle_compress_seconds=idle_compress_seconds,
            compress_model=compress_model,
            disabled_skills=disabled_skills,
        )

        # Dispatcher (accept pre-built or auto-discover agents)
        if dispatcher is not None:
            self._dispatcher = dispatcher
        else:
            from agents import discover_agents

            agents = discover_agents(provider)
            self._dispatcher = Dispatcher(
                agents, provider=provider, classify_model=compress_model
            )

        # Tools
        self._tools = ToolRegistry()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register built-in tools. Override in subclasses to add defaults."""
        ...

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    async def _ainput(prompt: str = "") -> str:
        """Async wrapper around :func:`input` with readline line-editing.

        Uses ``run_in_executor`` so the event loop is not blocked while
        waiting for user input.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return input(prompt)
        return await loop.run_in_executor(None, input, prompt)

    # -- interactive loop ------------------------------------------------------

    async def run(
        self,
        session_key: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        goal: str | None = None,
        skills: list[str] | None = None,
    ) -> None:
        """Continuous interactive loop — reads stdin, processes, prints results.

        Waits for user input on stdin with a ``>>>`` prompt.  Uses readline
        for line-editing (arrow keys, backspace, history).  When the idle
        time between messages exceeds ``idle_compress_seconds``, older
        session messages are automatically compressed by
        :class:`ContextManager`.

        Type ``/exit`` or ``/quit`` (or send EOF/Ctrl+D) to stop the loop.
        """
        PROMPT = ">>> "

        self._running = True

        try:
            while self._running:
                # Read input with line-editing support
                try:
                    line = await self._ainput(PROMPT)
                except EOFError:
                    # Ctrl+D
                    print()
                    break

                user_input = line.strip()
                if not user_input:
                    continue

                if user_input.lower() in ("/exit", "/quit"):
                    break

                # Process the single message
                result = await self._process_once(
                    session_key,
                    user_input,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    goal=goal,
                    skills=skills,
                )

                if result.error:
                    print(f"\nError [{result.paradigm}]: {result.error}")
                elif result.content:
                    print()  # trailing newline after streamed content

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            self._running = False
            logger.info("Orchestrator loop ended for session {!r}", session_key)

    # -- single-message processing --------------------------------------------

    async def _process_once(
        self,
        session_key: str,
        user_input: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        goal: str | None = None,
        skills: list[str] | None = None,
    ) -> OrchestratorResult:
        """Execute a single agent run for *user_input*.

        Lifecycle: resolve skills → build context → resolve paradigm
        → agent.run → save session → return result.
        """
        if not user_input.strip():
            raise ValueError("user_input must not be empty")

        paradigm: str = "unknown"

        try:
            # 1. Resolve active skills
            active_skills = list(skills or [])

            # 2. Build messages (includes repair, idle compression, token-budget compression)
            messages = self.ctx.build_messages(
                session_key,
                user_input,
                tools=self._tools,
                skills=active_skills or None,
            )

            # 3. Resolve paradigm
            paradigm = await self._dispatcher.resolve(user_input)

            # 4. Build spec with streaming callbacks
            _stream_started = False

            async def _on_delta(delta: str) -> None:
                nonlocal _stream_started
                if not _stream_started:
                    print()  # spacing after prompt
                    _stream_started = True
                print(delta, end="", flush=True)

            spec = AgentInput(
                init_messages=messages,
                tools=self._tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                goal=goal,
                on_content_delta=_on_delta,
            )

            # 5. Run agent (interruptible)
            try:
                output = await self._dispatcher.agents[paradigm].run(spec)
            except KeyboardInterrupt:
                logger.warning(
                    "Session {!r} interrupted by user", session_key
                )
                # Save partial state so the conversation can be resumed
                partial = list(messages)
                partial.append({
                    "role": "system",
                    "content": "[Session interrupted by user]",
                })
                self.ctx.session.set_messages(session_key, partial)
                raise

            # 6. Save session
            self.ctx.save_session(session_key, output.messages)

            return OrchestratorResult(
                content=output.content,
                session_key=session_key,
                paradigm=paradigm,
                usage=output.usage,
                stop_reason=output.stop_reason,
                error=output.error,
            )

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.opt(exception=True).error(
                "Orchestrator._process_once() failed for {!r}", session_key
            )
            return OrchestratorResult(
                content="",
                session_key=session_key,
                paradigm=paradigm,
                usage={},
                stop_reason="error",
                error=str(exc),
            )

    # -- delegation -----------------------------------------------------------

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.ctx.list_sessions()

    def delete_session(self, key: str) -> bool:
        """Delete a session and its on-disk data."""
        return self.ctx.delete_session(key)

    def remember(
        self,
        name: str,
        content: str,
        *,
        mem_type: str = "user",
        description: str = "",
    ) -> None:
        """Create or update a long-term memory entry."""
        self.ctx.remember(name, content, mem_type=mem_type, description=description)

    def forget(self, name: str) -> bool:
        """Delete a long-term memory entry."""
        return self.ctx.forget(name)

    def recall(self, query: str, *, top_n: int = 10) -> list[Any]:
        """Search long-term memories by keyword."""
        return self.ctx.recall(query, top_n=top_n)

    @property
    def tools(self) -> ToolRegistry:
        """The tool registry (mutable — use ``register_tool`` to populate)."""
        return self._tools

    def register_tool(self, tool: Tool) -> None:
        """Register a tool for agent use."""
        self._tools.register(tool)

    def unregister_tool(self, name: str) -> None:
        """Remove a previously registered tool."""
        self._tools.unregister(name)

    @property
    def dispatcher(self) -> Dispatcher:
        """The internal :class:`Dispatcher`."""
        return self._dispatcher

    @property
    def context(self) -> ContextManager:
        """The internal :class:`ContextManager`."""
        return self.ctx


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key=Config.api_key,
        api_base=Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )

    orche = Orchestrator(
        workspace=Config.workspace,
        provider=provider,
        compress_model=Config.light_model,
    )

    asyncio.run(orche.run("default"))
