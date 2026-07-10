"""Human-in-the-loop service and middleware for tool execution authorization.

Provides:
- ``HitlService``: asyncio.Future-based confirmation mechanism shared across channels
- ``HitlMiddleware``: AgentMiddleware that intercepts tool execution for user approval
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from config import Config
from core.middleware import AgentMiddleware, MiddlewareContext
from tools.guard import Capability
from tools.registry import ToolResult

# ---------------------------------------------------------------------------
# Confirmation targets
# ---------------------------------------------------------------------------

_CONFIRMABLE_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.SHELL,
    Capability.FILE_WRITE,
    Capability.NETWORK,
    Capability.DELEGATE,
})

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


@dataclass
class HitlRequest:
    """A pending HITL confirmation request."""

    request_id: str
    session_key: str
    tool_name: str
    arguments: dict[str, Any]
    capabilities: set[str]
    created_at: float = field(default_factory=time.monotonic)
    future: asyncio.Future[str] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future(),
    )


# ---------------------------------------------------------------------------
# HitlService
# ---------------------------------------------------------------------------


class HitlService:
    """Cross-channel HITL confirmation service.

    Each pending confirmation is backed by an ``asyncio.Future`` that
    channels resolve via :meth:`respond`.  Timeout is enforced by the
    service so no channel-side timeout logic is needed.

    Channels register callbacks via :meth:`add_listener` to be notified when
    a new confirmation request arrives.
    """

    def __init__(
        self,
        timeout_seconds: int = 120,
    ) -> None:
        self._timeout = timeout_seconds
        self._pending: dict[str, HitlRequest] = {}  # request_id → HitlRequest
        self._by_session: dict[str, list[str]] = {}  # session_key → [request_id, ...]
        self._listeners: list[Any] = []
        """Registered ``(HitlRequest) -> None`` callables, invoked when a
        new HITL confirmation is requested."""

    def add_listener(self, callback: Any) -> None:
        """Register *callback* to be notified of new HITL requests.

        *callback* may be sync or async.  It receives the :class:`HitlRequest`.
        """
        self._listeners.append(callback)

    # -- public API ----------------------------------------------------------

    @property
    def pending_requests(self) -> dict[str, HitlRequest]:
        """Snapshot of all currently pending requests."""
        return dict(self._pending)

    async def request_confirmation(
        self,
        session_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        capabilities: set[str],
    ) -> str:
        """Block until the user approves, denies, or the timeout fires.

        Returns ``"approved"``, ``"denied"``, or ``"timeout"``.
        """
        req = HitlRequest(
            request_id=uuid.uuid4().hex[:12],
            session_key=session_key,
            tool_name=tool_name,
            arguments=arguments,
            capabilities=capabilities,
        )
        self._pending[req.request_id] = req
        self._by_session.setdefault(session_key, []).append(req.request_id)

        logger.info(
            "HITL request {!r}: session={!r} tool={!r} caps={}",
            req.request_id, session_key, tool_name, capabilities,
        )

        # Notify all listeners so channels can show prompts
        for listener in self._listeners:
            try:
                result = listener(req)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.opt(exception=True).warning(
                    "HITL listener failed for {!r}", req.request_id,
                )

        try:
            result = await asyncio.wait_for(req.future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("HITL request {!r} timed out after {}s", req.request_id, self._timeout)
            return "timeout"
        finally:
            self._cleanup(req.request_id)

    def respond(self, request_id: str, decision: str) -> bool:
        """Resolve a pending request with *decision* (``"approved"`` or ``"denied"``).

        Returns ``True`` if the request was found and resolved, ``False`` otherwise.
        """
        req = self._pending.get(request_id)
        if req is None:
            return False
        if req.future.done():
            return False
        decision = decision if decision in ("approved", "denied") else "denied"
        req.future.set_result(decision)
        logger.info("HITL request {!r} resolved: {}", request_id, decision)
        return True

    # -- internal ------------------------------------------------------------

    def _cleanup(self, request_id: str) -> None:
        req = self._pending.pop(request_id, None)
        if req is None:
            return
        ids = self._by_session.get(req.session_key, [])
        if request_id in ids:
            ids.remove(request_id)
        if not ids:
            self._by_session.pop(req.session_key, None)


# ---------------------------------------------------------------------------
# HitlMiddleware
# ---------------------------------------------------------------------------


class HitlMiddleware(AgentMiddleware):
    """Middleware that pauses tool execution for user confirmation.

    Default mode is ``"confirm"`` — dangerous tools require user approval.
    Set ``HITL_MODE=bypass`` to auto-execute all tools without confirmation.
    """

    def __init__(
        self,
        service: HitlService,
        *,
        mode: str = "bypass",
        bypass_tools: set[str] | None = None,
    ) -> None:
        self._service = service
        self._mode = mode
        self._bypass_tools = bypass_tools or set()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    async def on_tool_execute(
        self, ctx: MiddlewareContext, call_next: Any,
    ) -> ToolResult:
        """Intercept tool execution for HITL confirmation."""
        tool_name = ctx.tool_name

        # Bypass: mode is bypass, or tool is explicitly bypassed
        if self._mode == "bypass" or tool_name in self._bypass_tools:
            return await call_next(ctx)

        # Check if tool has confirmable capabilities
        registry = ctx.tools
        tool = registry.get(tool_name) if registry is not None else None
        caps = tool.capabilities if tool else set()

        if not _needs_confirmation(caps):
            return await call_next(ctx)

        logger.info(
            "HITL: tool={!r} caps={} requires confirmation", tool_name, caps,
        )

        decision = await self._service.request_confirmation(
            session_key=ctx.session_key,
            tool_name=tool_name,
            arguments=dict(ctx.tool_arguments),
            capabilities={c.value for c in caps},
        )

        if decision == "approved":
            ctx.data["hitl_decision"] = "approved"
            return await call_next(ctx)

        if decision == "timeout":
            logger.warning(
                "HITL: tool={!r} timed out waiting for user confirmation", tool_name,
            )
        else:
            logger.info("HITL: tool={!r} denied by user", tool_name)

        ctx.data["hitl_decision"] = decision
        return ToolResult(
            success=False,
            content="",
            error=f"User did not approve tool '{tool_name}': {decision}",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _needs_confirmation(capabilities: set[Capability]) -> bool:
    """Return True if any capability triggers HITL confirmation."""
    return bool(capabilities & _CONFIRMABLE_CAPABILITIES)


def create_hitl_service_and_middleware() -> tuple[HitlService, HitlMiddleware]:
    """Factory: create HITL service + middleware from Config.

    Reads ``HITL_MODE``, ``HITL_BYPASS_TOOLS``, ``HITL_TIMEOUT_SECONDS``
    from :class:`Config`.
    """
    mode = Config.hitl_mode.strip().lower()
    if mode not in ("bypass", "confirm"):
        logger.warning(
            "Unknown HITL_MODE={!r}, falling back to 'confirm'", mode,
        )
        mode = "confirm"

    bypass_raw = Config.hitl_bypass_tools.strip()
    bypass_tools = {
        t.strip() for t in bypass_raw.split(",") if t.strip()
    } if bypass_raw else set()

    timeout = Config.hitl_timeout_seconds

    service = HitlService(timeout_seconds=timeout)
    middleware = HitlMiddleware(service, mode=mode, bypass_tools=bypass_tools)

    logger.info(
        "HITL initialized: mode={!r} timeout={}s bypass_tools={}",
        mode, timeout, sorted(bypass_tools) if bypass_tools else "none",
    )
    return service, middleware
