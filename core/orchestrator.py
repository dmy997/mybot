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
from observability.subscribers import install as install_subscribers
from observability.trace import tracer
from tools import ToolRegistry
from tools.mcp.client_manager import MCPClientManager
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
        mcp_config_path: str | Path | None = None,
    ) -> None:
        self._running = False
        self.workspace = Path(workspace).expanduser().resolve()

        # Observability — configure loguru + event bus subscribers once
        init_logging(log_config)
        install_subscribers(debug=(log_config is not None and log_config.level == "DEBUG"))

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
                    self.ctx.session.set_messages(session_key, partial)
                    raise
                except KeyboardInterrupt:
                    logger.warning("Session {!r} interrupted by user", session_key)
                    partial = list(messages)
                    partial.append({
                        "role": "system",
                        "content": "[Session interrupted by user]",
                    })
                    self.ctx.session.set_messages(session_key, partial)
                    raise

                if on_thinking_done:
                    await on_thinking_done()

                if on_tool_end:
                    for ev in output.tool_events:
                        await on_tool_end(ev)

                # 6. Save session — append only the new exchange
                assistant_msgs = output.messages[len(messages):]
                self.ctx.save_exchange(session_key, user_input, assistant_msgs)

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
                raw = await queue.get()
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
    import os
    import select
    import sys
    import uuid
    from contextlib import suppress

    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    from config import Config
    from core.message_bus import InboundMessage, MessageBus
    from observability.display import (
        console,
        print_error,
        show_banner,
        show_history,
        show_llm_usage,
        show_sessions,
    )
    from observability.stream_renderer import StreamRenderer
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

    bus_msg = MessageBus()

    # -- startup banner -------------------------------------------------------
    session_key = "default"
    session = orche.ctx.session.get_session(session_key)
    model = Config.default_model
    show_banner(
        session_key=session_key,
        model=model or "(provider default)",
        msg_count=len(session.messages),
        agents=list(orche.dispatcher.agents.keys()),
    )

    # -- prompt_toolkit session -----------------------------------------------
    _prompt_session: PromptSession | None = None

    def _init_prompt_session() -> None:
        nonlocal _prompt_session
        history_file = (
            orche.workspace / ".cli_history"
        )
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

    # -- synchronization ------------------------------------------------------
    _last_paradigm: str = "react"
    _processing_done = asyncio.Event()
    _processing_done.set()

    # -- CLI consumer ---------------------------------------------------------

    async def _consume_outbound(renderer: StreamRenderer) -> None:
        """Read outbound messages and route to StreamRenderer."""
        while True:
            raw = await bus_msg.outbound.get()
            if raw is None:
                break
            out: OutboundMessage = raw

            if out.msg_type == "thinking":
                pass  # chain-of-thought reasoning, not for display

            elif out.msg_type == "thinking_done":
                pass

            elif out.msg_type == "delta":
                await renderer.on_delta(out.data)

            elif out.msg_type == "tool_start":
                with renderer.pause_spinner():
                    renderer.ensure_header()
                    renderer.console.print(
                        f"  [dim cyan][tool:{out.data}][/dim cyan]",
                        highlight=False,
                    )

            elif out.msg_type == "tool_exec_start":
                d = out.data
                with renderer.pause_spinner():
                    renderer.ensure_header()
                    from observability.display import print_tool_progress_start
                    print_tool_progress_start(
                        d["name"], d.get("args", {}),
                        d.get("index", 1), d.get("total", 1),
                    )

            elif out.msg_type == "tool_exec_end":
                with renderer.pause_spinner():
                    renderer.ensure_header()
                    from observability.display import print_tool_progress_end
                    print_tool_progress_end(out.data)

            elif out.msg_type == "tool_end":
                pass  # already shown inline via tool_exec_start/end

            elif out.msg_type == "final":
                data = out.data or {}
                content = data.get("content", "")
                if renderer.streamed:
                    await renderer.on_end()
                else:
                    # No streaming deltas — print content directly
                    await renderer.close()
                    if content:
                        print()
                        from observability.display import render_content
                        render_content(content)
                print()
                show_llm_usage(data.get("usage", {}), data.get("elapsed_ms", 0), 0)
                if data.get("paradigm"):
                    _last_paradigm = data["paradigm"]
                _processing_done.set()

            elif out.msg_type == "error":
                await renderer.close()
                print()
                print_error(out.data)
                print()
                _processing_done.set()

    # -- interactive loop -----------------------------------------------------

    async def _run() -> None:
        nonlocal _prompt_session

        _init_prompt_session()
        await orche.start_mcp()
        serve_task = asyncio.create_task(orche.serve(bus_msg, session_key))

        renderer: StreamRenderer | None = None
        consumer_task: asyncio.Task[None] | None = None

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

                _processing_done.clear()
                if consumer_task:
                    consumer_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer_task

                renderer = StreamRenderer(bot_name="mybot")
                consumer_task = asyncio.create_task(_consume_outbound(renderer))

                await bus_msg.inbound(session_key).put(InboundMessage(
                    session_key=session_key,
                    content=user_input,
                    source="cli",
                    correlation_id=uuid.uuid4().hex,
                ))

                await _processing_done.wait()

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            if consumer_task:
                consumer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await consumer_task
            if renderer:
                await renderer.close()
            await bus_msg.inbound(session_key).put(None)
            serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await serve_task
            await orche.stop_mcp()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
