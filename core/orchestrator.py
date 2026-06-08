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
from pathlib import Path
from typing import Any

from loguru import logger

from context.context_manager import ContextManager
from observability import AgentRunEvent, LogConfig, SessionEvent, emit, init_logging
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

            agents = discover_agents(provider)
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

    def _print_startup_banner(
        self, session_key: str, model: str | None
    ) -> None:
        """Print session info and available commands on first entry."""
        session = self.ctx.session.get_session(session_key)
        msg_count = len(session.messages)
        print(f"\n  session : {session_key}")
        print(f"  model   : {model or '(provider default)'}")
        print(f"  history : {msg_count} 条消息")
        print(f"  agents  : {', '.join(self._dispatcher.agents.keys())}")
        print()
        print("  /help     显示帮助")
        print("  /history  显示对话摘要")
        print("  /clear    清屏")
        print("  /sessions 列出所有会话")
        print("  /exit     退出")
        print()

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
            print("\033[2J\033[H", end="")  # ANSI clear screen
            return True

        if name == "/sessions":
            await self._cmd_sessions()
            return True

        return False

    async def _cmd_history(self, session_key: str) -> None:
        """Print a summary of the current session's conversation."""
        session = self.ctx.session.get_session(session_key)
        messages = session.messages
        if not messages:
            print("  (暂无对话历史)")
            return
        print(f"\n  --- {session_key} 对话历史 ({len(messages)} 条消息) ---")
        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:120].replace("\n", " ")
                if len(content) > 120:
                    preview += "..."
            else:
                preview = f"[{type(content).__name__}]"
            print(f"  {i:3d}  {role:12s}  {preview}")
        print()

    async def _cmd_sessions(self) -> None:
        """List all saved sessions."""
        sessions = self.sessions
        if not sessions:
            print("  (暂无保存的会话)")
            return
        print(f"\n  --- 所有会话 ({len(sessions)} 个) ---")
        for s in sessions:
            key = s.get("key", "?")
            msg_count = s.get("message_count", 0)
            created = s.get("created_at", "?")
            print(f"  {key:20s}  {msg_count:4d} 条消息  创建于 {created}")
        print()

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

                # Read input with line-editing support
                try:
                    line = await self._ainput(prompt)
                except EOFError:
                    # Ctrl+D
                    print()
                    break

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
                    print(f"\nError [{result.paradigm}]: {result.error}")
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

                # 2. Build messages (includes repair, idle compression, token-budget compression)
                with tracer.span("context.build"):
                    messages = self.ctx.build_messages(
                        session_key,
                        user_input,
                        tools=self._tools,
                        skills=active_skills or None,
                    )

                # 3. Resolve paradigm
                with tracer.span("dispatcher.resolve"):
                    paradigm = await self._dispatcher.resolve(user_input)

                # 4. Build spec with streaming callbacks + dynamic thinking timer
                _stream_started = False
                _thinking_task: asyncio.Task[None] | None = None

                async def _run_thinking_timer() -> None:
                    """Update the thinking indicator in-place every 100 ms."""
                    start = time.monotonic()
                    try:
                        while True:
                            elapsed = time.monotonic() - start
                            print(f"\r  ⏳ 思考中 ({elapsed:.1f}s)  ", end="", flush=True)
                            await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        pass

                def _stop_thinking() -> None:
                    nonlocal _thinking_task
                    if _thinking_task is not None:
                        _thinking_task.cancel()
                        _thinking_task = None
                        print("\r\033[K", end="")  # clear the timer line

                async def _on_delta(delta: str) -> None:
                    nonlocal _stream_started
                    _stop_thinking()
                    if not _stream_started:
                        print()  # spacing after prompt
                        _stream_started = True
                    print(delta, end="", flush=True)

                async def _on_tool_call_delta(tc: dict[str, Any]) -> None:
                    nonlocal _stream_started
                    name = tc.get("name", "?")
                    _stop_thinking()
                    if not _stream_started:
                        print()  # spacing after prompt
                        _stream_started = True
                    print(f"  [tool:{name}] 执行中...", flush=True)

                # Start the dynamic thinking timer
                _thinking_task = asyncio.create_task(_run_thinking_timer())

                spec = AgentInput(
                    init_messages=messages,
                    tools=self._tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    goal=goal,
                    on_content_delta=_on_delta,
                    on_tool_call_delta=_on_tool_call_delta,
                )

                # 5. Run agent (interruptible)
                try:
                    with tracer.span(f"agent.{paradigm}.run"):
                        output = await self._dispatcher.agents[paradigm].run(spec)
                        steps = len(output.tool_events)
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
                    REGISTRY.agent_errors_total.inc()
                    raise
                finally:
                    _stop_thinking()  # safety: cancel timer if still running

                # 6. Save session
                self.ctx.save_session(session_key, output.messages)

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

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.opt(exception=True).error(
                    "Orchestrator._process_once() failed for {!r}", session_key
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    # --debug flag enables DEBUG-level console logging
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
