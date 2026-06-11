"""Async message bus for decoupled I/O between input sources, agent, and output sinks.

Two queues bridge the gap between external channels and the orchestrator:

- **Inbound** (per-session ``asyncio.Queue``): external input → agent.
  CLI, HTTP, Telegram, etc. put :class:`InboundMessage`; the orchestrator
  reads them via :meth:`get_inbound`.

- **Outbound** (shared ``asyncio.Queue``): agent → external UI / channels.
  The orchestrator puts :class:`OutboundMessage`; display, SSE, WebSocket
  consumers read them.  Consumers filter by ``correlation_id`` to pick up
  only messages belonging to their request.

Usage::

    bus = MessageBus()

    # Producer (CLI input loop)
    await bus.inbound("default").put(InboundMessage(
        session_key="default", content="hello", source="cli",
        correlation_id="abc123",
    ))

    # Consumer (orchestrator serve loop)
    msg = await bus.inbound("default").get()
    # ... process ...
    await bus.outbound.put(OutboundMessage(
        session_key="default", correlation_id="abc123",
        msg_type="final", data={"content": "hi!"},
    ))

    # Consumer (CLI output loop)
    out = await bus.outbound.get()
    if out.correlation_id == "abc123":
        print(out.data)
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """External input addressed to a session.

    Producers (CLI, HTTP, Telegram) create one of these per user utterance
    and put it on :meth:`MessageBus.inbound`.
    """

    session_key: str
    content: str
    source: str = ""  # "cli" | "http" | "websocket" | "telegram"
    correlation_id: str = ""
    model: str | None = None
    goal: str | None = None
    skills: list[str] | None = None
    timestamp: float = field(default_factory=_time.monotonic)


@dataclass
class OutboundMessage:
    """Agent output destined for a UI / external channel.

    The orchestrator puts one per streaming token, tool event, or final
    response.  Consumers filter by ``correlation_id`` to match the original
    inbound request.
    """

    session_key: str
    correlation_id: str
    msg_type: str
    """One of ``delta``, ``thinking``, ``thinking_done``, ``tool_start``,
    ``tool_end``, ``final``, ``error``."""

    data: Any
    """Payload matching *msg_type*:

    - ``delta`` / ``thinking``: ``str`` (single token)
    - ``thinking_done``: ``None``
    - ``tool_start``: ``str`` (tool name)
    - ``tool_end``: ``dict`` (name, status, duration_ms, detail)
    - ``final``: ``dict`` (content, usage, stop_reason)
    - ``error``: ``str`` (error message)
    """

    timestamp: float = field(default_factory=_time.monotonic)


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------


class MessageBus:
    """Two-queue message bus for agent I/O decoupling.

    Parameters
    ----------
    outbound_maxsize:
        Max queued outbound messages before backpressure is applied to the
        orchestrator (default 256).
    inbound_maxsize:
        Max queued inbound messages per session (default 64).
    """

    def __init__(
        self,
        *,
        outbound_maxsize: int = 256,
        inbound_maxsize: int = 64,
    ) -> None:
        self._outbound = asyncio.Queue[OutboundMessage](maxsize=outbound_maxsize)
        self._inbound: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._inbound_maxsize = inbound_maxsize

    # -- inbound (per-session) ----------------------------------------------

    def inbound(self, session_key: str) -> asyncio.Queue[InboundMessage]:
        """Return (creating if necessary) the per-session inbound queue.

        Each session has its own queue so a slow session never blocks
        messages addressed to another session.
        """
        if session_key not in self._inbound:
            self._inbound[session_key] = asyncio.Queue[InboundMessage](
                maxsize=self._inbound_maxsize,
            )
        return self._inbound[session_key]

    @property
    def sessions(self) -> list[str]:
        """Active session keys that have inbound queues."""
        return list(self._inbound.keys())

    def remove_session(self, session_key: str) -> None:
        """Discard the inbound queue for *session_key* (idle cleanup)."""
        self._inbound.pop(session_key, None)

    # -- outbound (shared) --------------------------------------------------

    @property
    def outbound(self) -> asyncio.Queue[OutboundMessage]:
        """Shared outbound queue — all sessions publish here."""
        return self._outbound

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        """Drain and clean up.  Puts a sentinel ``None`` on inbound queues."""
        for q in self._inbound.values():
            await q.put(None)  # type: ignore[arg-type]
