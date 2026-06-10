"""Orchestrator — top-level coordination layer.

Wires ContextManager, Dispatcher, and Agents together. Handles:
- Request lifecycle (build context → route → execute → persist)
- Continuous interactive loop (:meth:`run`)
- Crash recovery (Ctrl+C)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

try:
    import readline  # noqa: F401 — enables arrow keys, backspace, history in input()
except ImportError:
    pass
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.middleware import MiddlewareChain

from loguru import logger

from context.context_manager import ContextManager
from observability import AgentRunEvent, LogConfig, SessionEvent, emit, init_logging
from observability.display import (
    clear_thinking_timer,
    console,
    print_error,
    print_stream_delta,
    print_thinking_timer,
    show_banner,
    show_history,
    show_llm_usage,
    show_sessions,
)
from observability.metrics import REGISTRY
from observability.trace import tracer
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
        log_config: LogConfig | None = None,
        middleware: MiddlewareChain | None = None,
    ) -> None:
        self._running = False
        self.workspace = Path(workspace).expanduser().resolve()

        # Observability — configure loguru once
        init_logging(log_config)

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

            agents = discover_agents(provider, middleware=middleware)
            self._dispatcher = Dispatcher(
                agents, provider=provider, classify_model=compress_model
            )

        # Tools — main agent gets full access guard
        from tools.guard import ToolGuard as _ToolGuard
        self._tools = ToolRegistry(
            guard=_ToolGuard(self.workspace, scope="core", allow_network=True, allow_shell=True),
        )
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Auto-discover and register tools available in the ``"core"`` scope."""
        from tools import discover_tools
        from tools.subagent import SubAgentTool

        all_tools = discover_tools(workspace=self.workspace)
        for name, tool in all_tools.items():
            if tool.available_in("core"):
                self._tools.register(tool)
            else:
                logger.debug("Tool {!r} skipped (not available in 'core' scope)", name)

        # Register the sub-agent delegation tool (needs provider + parent registry)
        self._tools.register(SubAgentTool(self.ctx.provider, self._tools, workspace=self.workspace))

        # Register memory tools (need context manager access)
        from tools.memory_tools import MemoryForgetTool, MemoryRecallTool, MemoryRememberTool
        self._tools.register(MemoryRememberTool(self.ctx))
        self._tools.register(MemoryRecallTool(self.ctx))
        self._tools.register(MemoryForgetTool(self.ctx))

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

    async def _idle_watchdog(self, session_key: str) -> None:
        """Background task that compresses idle sessions during input wait.

        Sleeps for ``idle_compress_seconds`` then triggers compression of
        older messages (keeping the 10 most recent).  Designed to run
        concurrently with :meth:`_ainput` — cancelled when input arrives.
        """
        timeout = self.ctx.idle_compress_seconds
        if timeout <= 0:
            return

        await asyncio.sleep(timeout)
        await self.ctx.compress(session_key, keep_recent=10)

    def _print_startup_banner(
        self, session_key: str, model: str | None
    ) -> None:
        """Print session info and available commands on first entry."""
        session = self.ctx.session.get_session(session_key)
        show_banner(
            session_key=session_key,
            model=model or "(provider default)",
            msg_count=len(session.messages),
            agents=list(self._dispatcher.agents.keys()),
        )

    async def _handle_command(
        self, cmd: str, session_key: str, model: str | None
    ) -> bool:
        """Handle a built-in slash command.  Returns True if handled."""
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()

        if name == "/help":
            self._print_startup_banner(session_key, model)
            return True

        if name == "/history":
            await self._cmd_history(session_key)
            return True

        if name == "/clear":
            console.clear()
            return True

        if name == "/sessions":
            await self._cmd_sessions()
            return True

        return False

    async def _cmd_history(self, session_key: str) -> None:
        """Print a summary of the current session's conversation."""
        session = self.ctx.session.get_session(session_key)
        show_history(session_key, session.messages)

    async def _cmd_sessions(self) -> None:
        """List all saved sessions."""
        show_sessions(self.sessions)

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

        Uses readline for line-editing (arrow keys, backspace, history).
        When the idle time between messages exceeds ``idle_compress_seconds``,
        older session messages are automatically compressed by
        :class:`ContextManager`.

        Type ``/exit`` or ``/quit`` (or send EOF/Ctrl+D) to stop the loop.
        """
        self._running = True
        self._last_paradigm: str = "react"  # updated after each _process_once
        session = self.ctx.session.get_session(session_key)
        emit(SessionEvent(session_key=session_key, action="resumed",
                          message_count=len(session.messages)))

        # Startup banner
        self._print_startup_banner(session_key, model)

        try:
            while self._running:
                prompt = f"[{session_key}] {self._last_paradigm} › "

                # Start idle watchdog — compresses session if user is idle too long
                watchdog = asyncio.create_task(
                    self._idle_watchdog(session_key)
                )

                # Read input with line-editing support
                try:
                    line = await self._ainput(prompt)
                except EOFError:
                    # Ctrl+D
                    print()
                    break
                finally:
                    watchdog.cancel()
                    try:
                        await watchdog
                    except asyncio.CancelledError:
                        pass

                user_input = line.strip()
                if not user_input:
                    continue

                # --- built-in commands ---
                if user_input.lower() in ("/exit", "/quit"):
                    break

                if user_input.lower().startswith("/"):
                    handled = await self._handle_command(
                        user_input, session_key, model
                    )
                    if handled:
                        continue
                    # If not handled, pass through to agent (unknown command)

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
                    print_error(f"[{result.paradigm}] {result.error}")
                elif result.content:
                    print()  # trailing newline after streamed content

                # Track paradigm for prompt display
                if result.paradigm and result.paradigm != "unknown":
                    self._last_paradigm = result.paradigm

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            self._running = False
            logger.info("Orchestrator loop ended for session {!r}", session_key)

    # -- single-message processing --------------------------------------------

    async def process_message(
        self,
        session_key: str,
        user_input: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        goal: str | None = None,
        skills: list[str] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_done: Callable[[], Awaitable[None]] | None = None,
        on_tool_start: Callable[[str], Awaitable[None]] | None = None,
        on_tool_end: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    ) -> OrchestratorResult:
        """Execute a single agent run for *user_input*.

        Lifecycle: resolve skills → build context → resolve paradigm
        → agent.run → save session → return result.

        Callbacks are used for streaming output.  When omitted (CLI mode),
        built-in rich display helpers are used automatically.
        """
        if not user_input.strip():
            raise ValueError("user_input must not be empty")

        # Determine mode: if any callback is provided, use callback mode
        _use_callbacks = any(
            x is not None for x in (on_delta, on_thinking, on_thinking_done,
                                     on_tool_start, on_tool_end)
        )

        paradigm: str = "unknown"
        steps = 0
        t_start = time.monotonic()

        with tracer.trace(
            "orchestrator.process",
            session_key=session_key,
            user_input=user_input[:200],
        ):
            try:
                # 1. Resolve active skills
                active_skills = list(skills or [])

                # 2. Build messages (includes repair, token-budget compression)
                with tracer.span("context.build"):
                    messages = await self.ctx.build_messages(
                        session_key,
                        user_input,
                        tools=self._tools,
                        skills=active_skills or None,
                    )

                # 3. Resolve paradigm
                with tracer.span("dispatcher.resolve"):
                    paradigm = await self._dispatcher.resolve(user_input)

                # 4. Build streaming callbacks (CLI or callback mode)
                _thinking_task: asyncio.Task[None] | None = None

                def _stop_thinking() -> None:
                    nonlocal _thinking_task
                    if _thinking_task is not None:
                        _thinking_task.cancel()
                        _thinking_task = None
                        clear_thinking_timer()

                # Tool-execution / new-turn callbacks (default: None)
                _on_tool_end_cli: Callable[..., Awaitable[None]] | None = None
                _on_new_turn_cli: Callable[..., Awaitable[None]] | None = None

                if _use_callbacks:
                    # --- callback mode (HTTP/WS) ---
                    _shown_tool_indices: set[int] = set()

                    async def _on_delta(delta: str) -> None:
                        _stop_thinking()
                        if on_delta:
                            await on_delta(delta)

                    async def _on_thinking_delta(token: str) -> None:
                        if on_thinking:
                            await on_thinking(token)

                    async def _on_tool_call_delta(tc: dict[str, Any]) -> None:
                        idx = tc.get("index", 0)
                        if idx in _shown_tool_indices:
                            return
                        _shown_tool_indices.add(idx)
                        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                        name = tc.get("name") or fn.get("name", "?")
                        _stop_thinking()
                        if on_tool_start:
                            await on_tool_start(name)

                    _content_cb = _on_delta
                    _thinking_cb = _on_thinking_delta
                    _tool_cb = _on_tool_call_delta
                    # Callback mode: tool-end and new-turn handled via user-provided callbacks
                    _on_tool_end_cli = None
                    _on_new_turn_cli = None

                else:
                    # --- CLI mode (rich display) ---
                    _stream_started = False

                    async def _run_thinking_timer() -> None:
                        start = time.monotonic()
                        try:
                            while True:
                                elapsed = time.monotonic() - start
                                print_thinking_timer(elapsed)
                                await asyncio.sleep(0.1)
                        except asyncio.CancelledError:
                            pass

                    async def _cli_on_delta(delta: str) -> None:
                        nonlocal _stream_started
                        _stop_thinking()
                        if not _stream_started:
                            print()
                            _stream_started = True
                        print_stream_delta(delta)

                    # Stop timer when tool calls arrive (LLM is done "thinking").
                    # Actual execution progress is shown via on_tool_execute_start/end.
                    async def _cli_on_tool_call_delta(tc: dict[str, Any]) -> None:
                        _stop_thinking()

                    # Per-turn: stop old timer, reset stream state, start new timer
                    # so each LLM response starts on its own line with a fresh timer.
                    async def _cli_on_new_turn() -> None:
                        nonlocal _stream_started, _thinking_task
                        _stop_thinking()
                        _stream_started = False
                        _thinking_task = asyncio.create_task(_run_thinking_timer())
                        print()

                    # Tool execution progress — shown inline as tools complete.
                    async def _cli_on_tool_execute_end(ev: dict[str, Any]) -> None:
                        from observability.display import print_tool_progress_end
                        print_tool_progress_end(ev)

                    _thinking_task = asyncio.create_task(_run_thinking_timer())
                    _content_cb = _cli_on_delta
                    _thinking_cb = None
                    _tool_cb = _cli_on_tool_call_delta
                    _on_tool_end_cli = _cli_on_tool_execute_end
                    _on_new_turn_cli = _cli_on_new_turn

                spec = AgentInput(
                    init_messages=messages,
                    tools=self._tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    goal=goal,
                    on_content_delta=_content_cb,
                    on_thinking_delta=_thinking_cb,
                    on_tool_call_delta=_tool_cb,
                    on_tool_execute_end=_on_tool_end_cli,
                    on_new_turn=_on_new_turn_cli,
                )

                # 5. Run agent (interruptible)
                try:
                    with tracer.span(f"agent.{paradigm}.run"):
                        output = await self._dispatcher.agents[paradigm].run(spec)
                        steps = len(output.tool_events)
                except asyncio.CancelledError:
                    logger.warning("Session {!r} cancelled by user", session_key)
                    partial = list(messages)
                    partial.append({
                        "role": "system",
                        "content": "[Session interrupted by user]",
                    })
                    self.ctx.session.set_messages(session_key, partial)
                    REGISTRY.agent_errors_total.inc()
                    raise
                except KeyboardInterrupt:
                    logger.warning("Session {!r} interrupted by user", session_key)
                    partial = list(messages)
                    partial.append({
                        "role": "system",
                        "content": "[Session interrupted by user]",
                    })
                    self.ctx.session.set_messages(session_key, partial)
                    REGISTRY.agent_errors_total.inc()
                    raise
                finally:
                    _stop_thinking()

                # Notify thinking completed (callback mode)
                if _use_callbacks and on_thinking_done:
                    await on_thinking_done()

                # Report tool results (callback mode: per-event; CLI: already shown inline)
                if _use_callbacks and on_tool_end:
                    for ev in output.tool_events:
                        await on_tool_end(ev)

                # Print token/latency summary (CLI only)
                if not _use_callbacks:
                    total_ms = (time.monotonic() - t_start) * 1000
                    show_llm_usage(output.usage, total_ms, steps)

                # 6. Save session — append only the new exchange
                assistant_msgs = output.messages[len(messages):]
                self.ctx.save_exchange(session_key, user_input, assistant_msgs)

                # Record metrics & event
                total_ms = (time.monotonic() - t_start) * 1000
                REGISTRY.agent_steps.observe(steps)
                emit(AgentRunEvent(
                    session_key=session_key,
                    paradigm=paradigm,
                    steps=steps,
                    total_latency_ms=round(total_ms, 3),
                    stop_reason=output.stop_reason,
                    error=output.error,
                ))

                return OrchestratorResult(
                    content=output.content,
                    session_key=session_key,
                    paradigm=paradigm,
                    usage=output.usage,
                    stop_reason=output.stop_reason,
                    error=output.error,
                )

            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.opt(exception=True).error(
                    "Orchestrator.process_message() failed for {!r}", session_key
                )
                REGISTRY.agent_errors_total.inc()
                return OrchestratorResult(
                    content="",
                    session_key=session_key,
                    paradigm=paradigm,
                    usage={},
                    stop_reason="error",
                    error=str(exc),
                )

    # Keep alias for internal use (run() method)
    _process_once = process_message

    # -- delegation -----------------------------------------------------------

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.ctx.list_sessions()

    def delete_session(self, key: str) -> bool:
        """Delete a session and its on-disk data."""
        ok = self.ctx.delete_session(key)
        if ok:
            emit(SessionEvent(session_key=key, action="deleted"))
        return ok

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
# Entry points
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the interactive chat loop."""
    import sys

    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    console_level = "DEBUG" if "--debug" in sys.argv else "WARNING"

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
        log_config=LogConfig(level=console_level),
    )

    asyncio.run(orche.run("default"))


if __name__ == "__main__":
    main()
