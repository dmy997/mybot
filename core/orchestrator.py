"""Orchestrator — top-level coordination layer.

Wires ContextManager, Dispatcher, and Agents together. Handles:
- Request lifecycle (build context → route → execute → persist)
- Continuous interactive loop (:meth:`run`)
- Crash recovery (Ctrl+C)
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.middleware import MiddlewareChain

# ---------------------------------------------------------------------------
# Helper — format tool args for inline display
# ---------------------------------------------------------------------------
import json as _json

from loguru import logger

from context.context_manager import ContextManager
from core.events import SessionCreated, bus
from core.message_bus import InboundMessage, MessageBus, OutboundMessage
from observability import LogConfig, init_logging
from observability.otel_bridge import auto_install as auto_install_otel
from observability.subscribers import install as install_subscribers
from observability.trace import tracer
from services.scheduled_tasks import ScheduledTaskService
from services.xiaohongshu import XIAOHONGSHU_SESSION_KEY, xiaohongshu_prompt
from tools import ToolRegistry
from tools.schedule_task import ScheduleTaskTool

from .background_service import BackgroundService
from .dispatcher import Dispatcher
from .mcp_service import MCPService
from .runner import AgentInput
from .session_context import SessionContext
from .session_context import reset as reset_session
from .session_context import set_current as set_session


def _summarize_tool_args(args_json: str) -> str:
    """Convert a JSON arguments string into a brief inline summary.

    ``{"command": "ls -la", "workdir": "/tmp"}`` → ``command=ls -la``
    """
    if not args_json or not args_json.strip():
        return ""
    try:
        args = _json.loads(args_json)
        if not isinstance(args, dict):
            s = str(args)
            return (s[:47] + "...") if len(s) > 50 else s
        parts = []
        for k, v in args.items():
            sv = str(v)
            if len(sv) > 50:
                sv = sv[:47] + "..."
            parts.append(f"{k}={sv}")
        return ", ".join(parts)
    except (_json.JSONDecodeError, TypeError):
        s = args_json.strip()
        return (s[:77] + "...") if len(s) > 80 else s


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
        max_context_tokens: int = 200_000,
        max_output_tokens: int = 20_000,
        warning_buffer_ratio: float = 0.11,
        auto_compact_buffer_ratio: float = 0.072,
        block_buffer_ratio: float = 0.017,
        idle_compress_seconds: int = 300,
        compress_ratio: float = 0.5,
        consolidation_ratio: float = 0.7,
        compress_model: str | None = None,
        dispatcher: Dispatcher | None = None,
        disabled_skills: list[str] | None = None,
        log_config: LogConfig | None = None,
        middleware: MiddlewareChain | None = None,
        mcp_config_path: str | Path | None = None,
        max_session_messages: int = 2000,
        session_ttl_days: int = 30,
    ) -> None:
        self._running = False
        self.workspace = Path(workspace).expanduser().resolve()
        self._compress_idle_seconds = idle_compress_seconds
        self._compressing_sessions: set[str] = set()
        self._session_ttl_days = session_ttl_days
        self._last_purge_ts = 0.0

        # Observability — configure loguru + event bus subscribers once
        init_logging(log_config)
        install_subscribers(debug=(log_config is not None and log_config.level == "DEBUG"))
        auto_install_otel(tracer)

        # Generate default settings.json if not present
        from config import Config
        from config.settings import generate_default_settings
        generate_default_settings()

        # Hybrid search store (graceful degradation if unavailable)
        hybrid_store = None
        if Config.hybrid_search_enabled:
            try:
                from memory.hybrid_store import HybridStore

                hybrid_store = HybridStore(
                    db_path=os.path.join(str(self.workspace), "memory", "search.db"),
                    embedding_model_name=Config.embedding_model,
                )
            except Exception:
                logger.warning(
                    "Hybrid search unavailable, falling back to substring search"
                )

        # Context (idle compression is handled by ContextManager)
        self.ctx = ContextManager(
            self.workspace,
            provider=provider,
            max_context_tokens=max_context_tokens,
            max_output_tokens=max_output_tokens,
            warning_buffer_ratio=warning_buffer_ratio,
            auto_compact_buffer_ratio=auto_compact_buffer_ratio,
            block_buffer_ratio=block_buffer_ratio,
            idle_compress_seconds=idle_compress_seconds,
            compress_ratio=compress_ratio,
            consolidation_ratio=consolidation_ratio,
            compress_model=compress_model,
            disabled_skills=disabled_skills,
            hybrid_store=hybrid_store,
            max_session_messages=max_session_messages,
            session_ttl_days=session_ttl_days,
        )

        # Dispatcher (accept pre-built or auto-discover agents)
        if dispatcher is not None:
            self._dispatcher = dispatcher
        else:
            from agents import discover_agents

            agents = discover_agents(
                provider, middleware=middleware, max_context_tokens=max_context_tokens
            )
            self._dispatcher = Dispatcher(
                agents, provider=provider, classify_model=compress_model
            )

        # Tools — main agent gets full access guard
        from tools.guard import ToolGuard as _ToolGuard
        self._tools = ToolRegistry(
            guard=_ToolGuard(self.workspace, scope="core", allow_network=True, allow_shell=True),
        )
        self._register_default_tools()

        # MCP (Model Context Protocol) integration — composition
        self._mcp = MCPService(self._tools)
        self._mcp.load_config(
            Path(mcp_config_path).expanduser().resolve() if mcp_config_path else None,
            self.workspace,
        )

        # Background services (cron + Dream + scheduled tasks) — composition
        self._bg = BackgroundService(
            self.workspace,
            self.ctx.store,
            provider,
            compress_model or "",
            on_run_agent=self._run_scheduled_agent,
        )
        self.cron = self._bg.cron  # backward-compat alias

        self._scheduled = self._bg.scheduled_tasks
        self._scheduled.seed_system_task(
            task_id="xiaohongshu",
            schedule="0 20 * * *",
            prompt=xiaohongshu_prompt(self.workspace),
            session_key=XIAOHONGSHU_SESSION_KEY,
            skills=["xiaohongshu"],
        )
        self._scheduled.load()
        self._tools.register(ScheduleTaskTool(self._scheduled))

    # -- single-message processing --------------------------------------------

    async def process_message(
        self,
        session_key: str,
        user_input: str,
        *,
        source: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        goal: str | None = None,
        skills: list[str] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_done: Callable[[], Awaitable[None]] | None = None,
        on_tool_start: Callable[[str, str], Awaitable[None]] | None = None,
        on_tool_end: Callable[[dict[str, str]], Awaitable[None]] | None = None,
        on_tool_execute_start: (
            Callable[[str, dict[str, Any], int, int], Awaitable[None]] | None
        ) = None,
        on_tool_execute_end: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None,
        on_new_turn: Callable[[], Awaitable[None]] | None = None,
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
            token = set_session(SessionContext(session_key, source))
            try:
                # 1. Resolve active skills
                active_skills = list(skills or [])
                # Merge in always-on skills (marked metadata.mybot.always: true)
                for _always_skill in self.ctx.skills_loader.get_always_skills():
                    if _always_skill not in active_skills:
                        active_skills.append(_always_skill)

                # 1.5 Resolve per-model context window
                from config import Config
                from config.settings import resolve_context_window

                effective_model = model or Config.default_model
                mwc = resolve_context_window(effective_model)

                # 2. Build messages (includes repair, token-budget compression)
                with tracer.span("context.build"):
                    messages = await self.ctx.build_messages(
                        session_key,
                        user_input,
                        tools=self._tools,
                        skills=active_skills or None,
                        context_window=mwc.context_window,
                        max_output_tokens=mwc.max_output_tokens,
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
                    args_str = fn.get("arguments", "") if isinstance(fn, dict) else ""
                    args_brief = _summarize_tool_args(args_str)
                    if on_tool_start:
                        await on_tool_start(name, args_brief)

                # Reset dedup index set on each new LLM turn so tool
                # calls across multiple turns all get an on_tool_start.
                _on_new_turn = on_new_turn

                async def _on_new_turn_wrapper() -> None:
                    _shown_tool_indices.clear()
                    if _on_new_turn:
                        await _on_new_turn()

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
                    on_new_turn=_on_new_turn_wrapper,
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
                if self.ctx.consolidator is not None:
                    try:
                        session = self.ctx.session.get_session(session_key)
                        if session:

                            async def _build_fn():
                                return await self.ctx.build_messages(
                                    session_key,
                                    "",
                                    tools=self._tools,
                                )

                            async def _consolidate_and_prune():
                                try:
                                    did_consolidate = await self.ctx.consolidator.maybe_consolidate(
                                        session, build_messages_fn=_build_fn,
                                    )
                                    if did_consolidate:
                                        self.ctx.session.prune_archived_messages(session_key)
                                except Exception:
                                    logger.opt(exception=True).warning(
                                        "Consolidation task failed for {!r}", session_key,
                                    )

                            asyncio.create_task(_consolidate_and_prune())
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
            finally:
                reset_session(token)

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

        _dropped_logged = False

        def _safe_put(msg: OutboundMessage, channel: str = "default") -> None:
            """Put *msg* on the per-channel outbound queue without blocking.

            Uses ``put_nowait`` to avoid deadlocking the orchestrator when
            no consumer is draining that channel's queue.  Drops the message
            (with a single warning) when the queue is full.
            """
            nonlocal _dropped_logged
            try:
                bus_msg.outbound(channel).put_nowait(msg)
            except asyncio.QueueFull:
                if not _dropped_logged:
                    logger.warning(
                        "Outbound queue full for channel {!r} (maxsize={}), "
                        "dropping messages for session {!r} — consumer may "
                        "have disconnected",
                        channel, bus_msg.outbound(channel).maxsize, session_key,
                    )
                    _dropped_logged = True

        await bus.publish(SessionCreated(session_key=session_key))

        try:
            while self._running:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    await self._compress_idle_sessions(session_key)
                    await self._purge_expired_sessions()
                    continue

                if raw is None:  # sentinel
                    break

                msg: InboundMessage = raw
                cid = msg.correlation_id
                channel = msg.source or "default"
                _dropped_logged = False  # reset per inbound message

                async def _on_delta(token: str) -> None:
                    _safe_put(OutboundMessage(session_key, cid, "delta", token), channel)

                async def _on_thinking(token: str) -> None:
                    _safe_put(OutboundMessage(session_key, cid, "thinking", token), channel)

                async def _on_thinking_done() -> None:
                    _safe_put(OutboundMessage(session_key, cid, "thinking_done", None), channel)

                async def _on_tool_start(name: str, args_brief: str = "") -> None:
                    _safe_put(OutboundMessage(session_key, cid, "tool_start", name), channel)

                async def _on_tool_end(ev: dict[str, str]) -> None:
                    _safe_put(OutboundMessage(session_key, cid, "tool_end", ev), channel)

                async def _on_tool_exec_start(
                    name: str, args: dict[str, Any], idx: int, total: int,
                ) -> None:
                    _safe_put(OutboundMessage(
                        session_key, cid, "tool_exec_start",
                        {"name": name, "args": args, "index": idx, "total": total}), channel)

                async def _on_tool_exec_end(ev: dict[str, Any]) -> None:
                    _safe_put(OutboundMessage(session_key, cid, "tool_exec_end", ev), channel)

                async def _on_new_turn() -> None:
                    _safe_put(OutboundMessage(session_key, cid, "new_turn", None), channel)

                t_start = time.monotonic()
                try:
                    result = await self.process_message(
                        session_key=session_key,
                        user_input=msg.content,
                        source=channel,
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
                        on_new_turn=_on_new_turn,
                    )
                    _safe_put(OutboundMessage(
                        session_key, cid, "final",
                        {"content": result.content, "usage": result.usage,
                         "stop_reason": result.stop_reason,
                         "paradigm": result.paradigm,
                         "elapsed_ms": (time.monotonic() - t_start) * 1000},
                    ), channel)
                except Exception as exc:
                    logger.opt(exception=True).error(
                        "serve() failed for session {!r}", session_key)
                    _safe_put(
                        OutboundMessage(session_key, cid, "error", str(exc)), channel)
        finally:
            self._running = False
            logger.info("serve() ended for session {!r}", session_key)

    # ========================================================================
    # Tool registry (inlined from ToolRegistryMixin)
    # ========================================================================

    def _register_default_tools(self) -> None:
        """Auto-discover and register tools available in the ``"core"`` scope."""
        from tools import discover_tools
        from tools.memory_tools import MemoryForgetTool, MemoryRecallTool, MemoryRememberTool
        from tools.subagent import SubAgentTool

        all_tools = discover_tools(workspace=self.workspace)
        for name, tool in all_tools.items():
            if tool.available_in("core"):
                self._tools.register(tool)
            else:
                logger.debug("Tool {!r} skipped (not available in 'core' scope)", name)

        self._tools.register(SubAgentTool(
            self.ctx.provider, self._tools, workspace=self.workspace,
        ))
        self._tools.register(MemoryRememberTool(self.ctx))
        self._tools.register(MemoryRecallTool(self.ctx))
        self._tools.register(MemoryForgetTool(self.ctx))

    def register_tool(self, tool: object) -> None:
        """Register a tool for agent use."""
        self._tools.register(tool)
        self.ctx._invalidate_static()

    def unregister_tool(self, name: str) -> None:
        """Remove a previously registered tool."""
        self._tools.unregister(name)
        self.ctx._invalidate_static()

    def get_tool(self, name: str) -> object | None:
        """Return a registered core-scope tool by name, or ``None``."""
        return self._tools.get(name)

    @property
    def tools(self) -> ToolRegistry:
        """The tool registry (mutable — use ``register_tool`` to populate)."""
        return self._tools

    # ========================================================================
    # Session lifecycle (inlined from SessionLifecycleMixin)
    # ========================================================================

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """List all saved sessions."""
        return self.ctx.list_sessions()

    def delete_session(self, key: str) -> bool:
        """Delete a session and its on-disk data."""
        ok: bool = self.ctx.delete_session(key)
        if ok:
            try:
                loop = asyncio.get_running_loop()
                from core.events import SessionDeleted, bus
                loop.create_task(bus.publish(SessionDeleted(session_key=key)))
            except RuntimeError:
                pass
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
    def context(self) -> ContextManager:
        """The internal :class:`ContextManager`."""
        return self.ctx

    @property
    def dispatcher(self) -> Dispatcher:
        """The internal :class:`Dispatcher`."""
        return self._dispatcher

    # ========================================================================
    # Idle compression (inlined from IdleCompressionMixin)
    # ========================================================================

    async def _compress_idle_sessions(self, active_session_key: str) -> None:
        """Scan all sessions and compress those that have been idle too long."""
        idle_seconds: int = self._compress_idle_seconds
        if idle_seconds <= 0:
            return

        try:
            now = time.time()
            for sess in self.ctx.list_sessions():
                key: str = sess.get("key", "")
                if not key:
                    continue
                if key == active_session_key:
                    continue
                if key in self._compressing_sessions:
                    continue

                updated_str: str = sess.get("updated_at", "")
                if not updated_str:
                    continue
                try:
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
            session = self.ctx.session.get_session(session_key)
            cursor = session.consolidated_cursor
            unsummarised = len(session.messages) - cursor
            if unsummarised <= 10:
                return

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

    async def _purge_expired_sessions(self) -> None:
        """Purge sessions that have been inactive beyond ``session_ttl_days``.

        Runs at most once per hour to avoid redundant filesystem scans.
        """
        now = time.time()
        if now - self._last_purge_ts < 3600:
            return
        self._last_purge_ts = now
        try:
            deleted: int = self.ctx.purge_expired_sessions()
            if deleted:
                logger.info("Purged {} expired session(s)", deleted)
        except Exception:
            logger.opt(exception=True).warning("Session expiry purge failed")

    # ========================================================================
    # Services (inlined from MCPServicesMixin)
    # ========================================================================

    async def _run_scheduled_agent(
        self, session_key: str, prompt: str, skills: list[str] | None,
    ) -> None:
        """Run an internal side-effect scheduled task through the agent pipeline."""
        await self.process_message(
            session_key=session_key,
            user_input=prompt,
            skills=skills,
            source="cron",
        )

    async def start_services(self) -> None:
        """Start background services (MCP + cron)."""
        await self._mcp.start()
        await self._bg.start()

    async def stop_services(self) -> None:
        """Stop background services (MCP + cron)."""
        self._bg.stop()
        await self._mcp.stop()

    @property
    def scheduled_tasks(self) -> ScheduledTaskService:
        """The unified scheduled-task service (chat-created + system tasks)."""
        return self._bg.scheduled_tasks


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point — launch the Textual chat UI."""
    import sys
    from datetime import datetime

    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider
    from tui import ChatApp

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
        max_context_tokens=Config.context_window,
        max_output_tokens=Config.max_output_tokens,
        warning_buffer_ratio=Config.warning_buffer_ratio,
        auto_compact_buffer_ratio=Config.auto_compact_buffer_ratio,
        block_buffer_ratio=Config.block_buffer_ratio,
        compress_ratio=Config.compress_ratio,
        consolidation_ratio=Config.consolidation_ratio,
        idle_compress_seconds=Config.idle_compress_seconds,
        compress_model=Config.light_model,
        log_config=LogConfig(level=console_level),
    )

    # -- session key ---------------------------------------------------------
    last_session_file = orche.workspace / ".last_session"

    if do_continue:
        if last_session_file.exists():
            session_key = last_session_file.read_text(encoding="utf-8").strip()
            if not session_key or not orche.ctx.session.get_session(session_key).messages:
                session_key = datetime.now().strftime("%Y%m%d-%H%M%S")
        else:
            session_key = datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        session_key = datetime.now().strftime("%Y%m%d-%H%M%S")

    last_session_file.parent.mkdir(parents=True, exist_ok=True)
    last_session_file.write_text(session_key, encoding="utf-8")

    model = Config.default_model
    session = orche.ctx.session.get_session(session_key)
    is_resumed = do_continue and bool(session.messages)

    # -- launch Textual app --------------------------------------------------
    async def _run() -> None:
        await orche.start_services()
        await bus.publish(SessionCreated(session_key=session_key))
        app = ChatApp(
            orchestrator=orche,
            session_key=session_key,
            model=model or "(provider default)",
            is_resumed=is_resumed,
        )
        await app.run_async()
        await orche.stop_services()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
