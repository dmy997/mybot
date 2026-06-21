"""Orchestrator — top-level coordination layer.

Wires ContextManager, Dispatcher, and Agents together. Handles:
- Request lifecycle (build context → route → execute → persist)
- Continuous interactive loop (:meth:`run`)
- Crash recovery (Ctrl+C)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.middleware import MiddlewareChain

from loguru import logger

from context.context_manager import ContextManager
from core.events import SessionCreated, SessionDeleted, bus
from core.message_bus import InboundMessage, MessageBus, OutboundMessage
from observability import LogConfig, init_logging
from observability.otel_bridge import auto_install as auto_install_otel
from observability.subscribers import install as install_subscribers
from observability.trace import tracer
from tools import ToolRegistry
from tools.mcp.client_manager import MCPClientManager
from tools.tool import Tool

from services.cron import CronScheduler
from memory.dream import Dream

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
        mcp_config_path: str | Path | None = None,
    ) -> None:
        self._running = False
        self.workspace = Path(workspace).expanduser().resolve()
        self._compress_idle_seconds = idle_compress_seconds
        self._compressing_sessions: set[str] = set()

        # Observability — configure loguru + event bus subscribers once
        init_logging(log_config)
        install_subscribers(debug=(log_config is not None and log_config.level == "DEBUG"))
        auto_install_otel(tracer)

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

        # MCP (Model Context Protocol) integration
        self._mcp_manager: MCPClientManager | None = None
        self._mcp_config_path: Path | None = (
            Path(mcp_config_path).expanduser().resolve() if mcp_config_path else None
        )
        self._setup_mcp()

        # Cron — self-driven periodic task scheduler
        cron_dir = self.workspace / "cron"
        self._dream = Dream(
            store=self.ctx.store,
            provider=provider,
            model=compress_model or "",
        )
        self.cron = CronScheduler(
            state_dir=cron_dir,
            on_job=self._on_cron_job,
        )
        self.cron.register_job("dream", interval_hours=2)

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

    # -- MCP -----------------------------------------------------------------

    def _setup_mcp(self) -> None:
        """Load MCP config (if any) and create the client manager but don't connect yet."""
        from tools.mcp.client_manager import load_mcp_config, MCPServerConfig  # noqa: F811

        config_path = self._mcp_config_path
        if config_path is None:
            default = self.workspace / "mcp_servers.json"
            if default.exists():
                config_path = default

        if config_path is not None and Path(config_path).exists():
            try:
                servers = load_mcp_config(config_path)
                if servers:
                    self._mcp_manager = MCPClientManager(self._tools)
                    self._mcp_manager.configure(servers)
                    logger.info(
                        "MCP config loaded from {!s}: {!s} server(s)",
                        config_path, len(servers),
                    )
            except Exception:
                logger.exception("Failed to load MCP config from {!s}", config_path)

    async def start_mcp(self) -> None:
        """Connect to configured MCP servers and register their tools."""
        if self._mcp_manager is not None:
            await self._mcp_manager.start()

    async def stop_mcp(self) -> None:
        """Disconnect all MCP servers and unregister their tools."""
        if self._mcp_manager is not None:
            await self._mcp_manager.stop()

    # -- Cron / Dream ---------------------------------------------------------

    async def _on_cron_job(self, name: str) -> None:
        """Route cron job *name* to the appropriate handler."""
        if name == "dream":
            await self._dream.run()

    async def start_services(self) -> None:
        """Start background services (MCP + cron)."""
        await self.start_mcp()
        await self.cron.start()

    async def stop_services(self) -> None:
        """Stop background services (MCP + cron)."""
        self.cron.stop()
        await self.stop_mcp()

    # -- helpers ---------------------------------------------------------------

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
        on_tool_execute_start: (
            Callable[[str, dict[str, Any], int, int], Awaitable[None]] | None
        ) = None,
        on_tool_execute_end: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None,
    ) -> OrchestratorResult:
        """Execute a single agent run for *user_input*.

        Lifecycle: resolve skills → build context → resolve paradigm
        → agent.run → save session → return result.

        Callbacks are used for streaming output to external consumers
        (HTTP/WS, CLI via MessageBus).  The caller is responsible for
        rendering or forwarding output appropriately.
        """
        if not user_input.strip():
            raise ValueError("user_input must not be empty")

        paradigm: str = "unknown"

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

                # 4. Build streaming callbacks
                _shown_tool_indices: set[int] = set()

                async def _on_delta(delta: str) -> None:
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
                    if on_tool_start:
                        await on_tool_start(name)

                spec = AgentInput(
                    init_messages=messages,
                    tools=self._tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    goal=goal,
                    session_key=session_key,
                    paradigm=paradigm,
                    on_content_delta=_on_delta,
                    on_thinking_delta=_on_thinking_delta,
                    on_tool_call_delta=_on_tool_call_delta,
                    on_tool_execute_start=on_tool_execute_start,
                    on_tool_execute_end=on_tool_execute_end,
                )

                # 5. Run agent (interruptible)
                try:
                    with tracer.span(f"agent.{paradigm}.run"):
                        output = await self._dispatcher.agents[paradigm].run(spec)
                except asyncio.CancelledError:
                    logger.warning("Session {!r} cancelled by user", session_key)
                    partial = list(messages)
                    partial.append({
                        "role": "system",
                        "content": "[Session interrupted by user]",
                    })
                    async with self.ctx.session.lock_session(session_key):
                        self.ctx.session.set_messages(session_key, partial)
                    raise
                except KeyboardInterrupt:
                    logger.warning("Session {!r} interrupted by user", session_key)
                    partial = list(messages)
                    partial.append({
                        "role": "system",
                        "content": "[Session interrupted by user]",
                    })
                    async with self.ctx.session.lock_session(session_key):
                        self.ctx.session.set_messages(session_key, partial)
                    raise

                if on_thinking_done:
                    await on_thinking_done()

                if on_tool_end:
                    for ev in output.tool_events:
                        await on_tool_end(ev)

                # 6. Save session — append only the new exchange
                assistant_msgs = output.messages[len(messages):]
                await self.ctx.save_exchange(session_key, user_input, assistant_msgs)

                # 7. Consolidate — fire-and-forget, never blocks the response
                try:
                    session = self.ctx.session.get_session(session_key)
                    if session:

                        async def _build_fn():
                            return await self.ctx.build_messages(
                                session_key,
                                "",
                                tools=self._tools,
                            )

                        asyncio.create_task(
                            self.ctx.consolidator.maybe_consolidate(
                                session, build_messages_fn=_build_fn,
                            )
                        )
                except Exception:
                    logger.opt(exception=True).warning(
                        "Consolidation skipped for {!r}", session_key,
                    )

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
                return OrchestratorResult(
                    content="",
                    session_key=session_key,
                    paradigm=paradigm,
                    usage={},
                    stop_reason="error",
                    error=str(exc),
                )

    # -- idle compression -------------------------------------------------------

    async def _compress_idle_sessions(self, active_session_key: str) -> None:
        """Scan all sessions and compress those that have been idle too long.

        Only sessions whose last update exceeds ``_compress_idle_seconds``
        are compressed.  The active session and sessions already being
        compressed are skipped.
        """
        idle_seconds = self._compress_idle_seconds
        if idle_seconds <= 0:
            return

        try:
            now = time.time()
            for sess in self.ctx.list_sessions():
                key = sess.get("key", "")
                if not key:
                    continue
                if key == active_session_key:
                    continue
                if key in self._compressing_sessions:
                    continue

                updated_str = sess.get("updated_at", "")
                if not updated_str:
                    continue
                try:
                    from datetime import datetime
                    updated_at = datetime.fromisoformat(updated_str)
                    updated_ts = updated_at.timestamp()
                except (ValueError, TypeError, OSError):
                    continue

                if now - updated_ts <= idle_seconds:
                    continue

                self._compressing_sessions.add(key)
                asyncio.create_task(
                    self._compress_stale_session(key),
                    name=f"idle-compress-{key}",
                )
        except Exception:
            logger.opt(exception=True).debug("Idle compression scan failed")

    async def _compress_stale_session(self, session_key: str) -> None:
        """Compress a stale session: keep the 10 most recent messages, summarise the rest."""
        try:
            # Load the full session to check message count
            session = self.ctx.session.get_session(session_key)
            cursor = session.consolidated_cursor
            unsummarised = len(session.messages) - cursor
            if unsummarised <= 10:
                return  # nothing to compress

            logger.info(
                "Idle compression: {!r} has {} unsummarised messages, compressing...",
                session_key, unsummarised,
            )
            n = await self.ctx.compress(session_key, keep_recent=10)
            if n > 0:
                logger.info(
                    "Idle compression: {!r} compressed {} messages (kept last 10)",
                    session_key, n,
                )
        except Exception:
            logger.opt(exception=True).warning(
                "Idle compression failed for {!r}", session_key,
            )
        finally:
            self._compressing_sessions.discard(session_key)

    # -- MessageBus-based serving ---------------------------------------------

    async def serve(self, bus_msg: MessageBus, session_key: str) -> None:
        """Continuously process inbound messages and write results to the outbound queue.

        Reads one :class:`InboundMessage` at a time from
        ``bus_msg.inbound(session_key)``, calls :meth:`process_message`, and
        publishes streaming output as :class:`OutboundMessage` on
        ``bus_msg.outbound``.

        Each outbound message carries the *correlation_id* from the inbound
        message that triggered it, so output consumers can filter by request.

        Sentinels: a ``None`` on the inbound queue causes the loop to exit.
        """
        queue = bus_msg.inbound(session_key)
        self._running = True

        await bus.publish(SessionCreated(session_key=session_key))

        try:
            while self._running:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    await self._compress_idle_sessions(session_key)
                    continue

                if raw is None:  # sentinel
                    break

                msg: InboundMessage = raw
                cid = msg.correlation_id

                async def _on_delta(token: str) -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "delta", token))

                async def _on_thinking(token: str) -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "thinking", token))

                async def _on_thinking_done() -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "thinking_done", None))

                async def _on_tool_start(name: str) -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "tool_start", name))

                async def _on_tool_end(ev: dict[str, str]) -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "tool_end", ev))

                async def _on_tool_exec_start(
                    name: str, args: dict[str, Any], idx: int, total: int,
                ) -> None:
                    await bus_msg.outbound.put(OutboundMessage(
                        session_key, cid, "tool_exec_start",
                        {"name": name, "args": args, "index": idx, "total": total}))

                async def _on_tool_exec_end(ev: dict[str, Any]) -> None:
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "tool_exec_end", ev))

                t_start = time.monotonic()
                try:
                    result = await self.process_message(
                        session_key=session_key,
                        user_input=msg.content,
                        model=msg.model,
                        temperature=msg.temperature,
                        max_tokens=msg.max_tokens,
                        goal=msg.goal,
                        skills=msg.skills,
                        on_delta=_on_delta,
                        on_thinking=_on_thinking,
                        on_thinking_done=_on_thinking_done,
                        on_tool_start=_on_tool_start,
                        on_tool_end=_on_tool_end,
                        on_tool_execute_start=_on_tool_exec_start,
                        on_tool_execute_end=_on_tool_exec_end,
                    )
                    await bus_msg.outbound.put(OutboundMessage(
                        session_key, cid, "final",
                        {"content": result.content, "usage": result.usage,
                         "stop_reason": result.stop_reason,
                         "paradigm": result.paradigm,
                         "elapsed_ms": (time.monotonic() - t_start) * 1000},
                    ))
                except Exception as exc:
                    logger.opt(exception=True).error(
                        "serve() failed for session {!r}", session_key)
                    await bus_msg.outbound.put(
                        OutboundMessage(session_key, cid, "error", str(exc)))
        finally:
            self._running = False
            logger.info("serve() ended for session {!r}", session_key)

    # -- delegation -----------------------------------------------------------

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.ctx.list_sessions()

    def delete_session(self, key: str) -> bool:
        """Delete a session and its on-disk data."""
        ok = self.ctx.delete_session(key)
        if ok:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(bus.publish(SessionDeleted(session_key=key)))
            except RuntimeError:
                pass  # no event loop (e.g. sync test), skip event
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
        self.ctx._invalidate_static()

    def unregister_tool(self, name: str) -> None:
        """Remove a previously registered tool."""
        self._tools.unregister(name)
        self.ctx._invalidate_static()

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
    import os
    import select
    import sys
    import time
    from contextlib import suppress
    from datetime import datetime
    from typing import Any

    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    from config import Config
    from observability.display import (
        console,
        print_error,
        print_tool_progress_end,
        print_tool_progress_start,
        render_content,
        show_banner,
        show_history,
        show_llm_usage,
        show_sessions,
    )
    from observability.stream_renderer import StreamRenderer
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    # -- CLI flags -----------------------------------------------------------
    do_continue = "-c" in sys.argv or "--continue" in sys.argv
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

    # -- session key ---------------------------------------------------------
    last_session_file = orche.workspace / ".last_session"

    if do_continue:
        if last_session_file.exists():
            session_key = last_session_file.read_text(encoding="utf-8").strip()
            if not session_key or not orche.ctx.session.get_session(session_key).messages:
                print("No previous session found or session is empty. Starting a new one.")
                session_key = datetime.now().strftime("%Y%m%d-%H%M%S")
        else:
            print("No previous session found. Starting a new one.")
            session_key = datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        session_key = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Persist session key for future --continue
    last_session_file.parent.mkdir(parents=True, exist_ok=True)
    last_session_file.write_text(session_key, encoding="utf-8")

    # -- startup banner -------------------------------------------------------
    session = orche.ctx.session.get_session(session_key)
    model = Config.default_model
    show_banner(
        session_key=session_key,
        model=model or "(provider default)",
        msg_count=len(session.messages),
        agents=list(orche.dispatcher.agents.keys()),
        resumed=do_continue and bool(session.messages),
    )

    # -- prompt_toolkit session -----------------------------------------------
    _prompt_session: PromptSession | None = None

    def _init_prompt_session() -> None:
        nonlocal _prompt_session
        history_file = orche.workspace / ".cli_history"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        _prompt_session = PromptSession(
            history=FileHistory(str(history_file)),
            enable_open_in_editor=False,
            multiline=False,
        )

    def _flush_pending_tty_input() -> None:
        """Drop unread keypresses typed while the agent was generating output."""
        try:
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
        except Exception:
            return
        with suppress(Exception):
            import termios
            termios.tcflush(fd, termios.TCIFLUSH)
            return
        with suppress(Exception):
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                if not os.read(fd, 4096):
                    break

    async def _read_input_async() -> str:
        """Read user input using prompt_toolkit."""
        if _prompt_session is None:
            raise RuntimeError("Call _init_prompt_session() first")
        try:
            with patch_stdout():
                return await _prompt_session.prompt_async(
                    HTML("<b fg='ansiblue'>You:</b> "),
                )
        except EOFError:
            raise KeyboardInterrupt from None

    _last_paradigm: str = "react"

    # -- interactive loop -----------------------------------------------------

    async def _run() -> None:
        nonlocal _prompt_session, _last_paradigm

        _init_prompt_session()
        await orche.start_services()
        await bus.publish(SessionCreated(session_key=session_key))

        renderer: StreamRenderer | None = None

        try:
            while True:
                _flush_pending_tty_input()
                if renderer:
                    renderer.stop_for_input()

                try:
                    line = await _read_input_async()
                except KeyboardInterrupt:
                    print("\nInterrupted.")
                    break

                user_input = line.strip()
                if not user_input:
                    continue

                if user_input.lower() in ("/exit", "/quit"):
                    print("Goodbye!")
                    break

                if user_input.lower().startswith("/"):
                    parts = user_input.split(maxsplit=1)
                    cmd = parts[0].lower()
                    if cmd == "/help":
                        show_banner(
                            session_key=session_key,
                            model=model or "(provider default)",
                            msg_count=len(orche.ctx.session.get_session(session_key).messages),
                            agents=list(orche.dispatcher.agents.keys()),
                        )
                        continue
                    if cmd == "/history":
                        session = orche.ctx.session.get_session(session_key)
                        show_history(session_key, session.messages)
                        continue
                    if cmd == "/clear":
                        console.clear()
                        continue
                    if cmd == "/sessions":
                        show_sessions(orche.sessions)
                        continue

                renderer = StreamRenderer(bot_name="mybot")

                # Direct callbacks — bypass MessageBus queue so deltas are
                # rendered synchronously in the chat_stream task without
                # inter-task scheduling latency.  Every other path (HTTP/WS
                # server) still uses the MessageBus; only CLI takes this
                # fast path.
                async def _on_delta(token: str) -> None:
                    await renderer.on_delta(token)

                async def _on_thinking(token: str) -> None:
                    pass  # spinner already visible

                async def _on_thinking_done() -> None:
                    pass

                async def _on_tool_start(name: str) -> None:
                    with renderer.pause_spinner():
                        renderer.ensure_header()
                        renderer.console.print(
                            f"  [dim cyan][tool:{name}][/dim cyan]",
                            highlight=False,
                        )

                async def _on_tool_exec_start(
                    name: str, args: dict[str, Any], idx: int, total: int,
                ) -> None:
                    with renderer.pause_spinner():
                        renderer.ensure_header()
                        print_tool_progress_start(
                            name, args, idx, total, _console=renderer.console,
                        )

                async def _on_tool_exec_end(ev: dict[str, Any]) -> None:
                    with renderer.pause_spinner():
                        renderer.ensure_header()
                        print_tool_progress_end(ev, _console=renderer.console)

                t_start = time.monotonic()
                try:
                    result = await orche.process_message(
                        session_key=session_key,
                        user_input=user_input,
                        on_delta=_on_delta,
                        on_thinking=_on_thinking,
                        on_thinking_done=_on_thinking_done,
                        on_tool_start=_on_tool_start,
                        on_tool_execute_start=_on_tool_exec_start,
                        on_tool_execute_end=_on_tool_exec_end,
                    )
                except Exception as exc:
                    await renderer.close()
                    renderer = None
                    print()
                    print_error(str(exc))
                    print()
                    continue

                # Final render
                if renderer.streamed:
                    await renderer.on_end()
                else:
                    await renderer.close()
                    if result.content:
                        print()
                        render_content(result.content)

                if result.error:
                    print()
                    print_error(result.error)
                    if result.stop_reason:
                        console.print(f"(stop_reason: {result.stop_reason})", style="dim")

                print()
                show_llm_usage(
                    result.usage,
                    (time.monotonic() - t_start) * 1000,
                    0,
                )
                if result.paradigm:
                    _last_paradigm = result.paradigm

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            if renderer:
                await renderer.close()
            await orche.stop_services()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
