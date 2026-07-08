"""Ambient session context for tool execution.

Tools receive only the arguments the LLM provides — not the session they run
in.  Some tools (e.g. ``schedule_task``) need to know *which* session and
channel invoked them so a scheduled task can deliver its result back to the
right place.

Rather than thread ``session_key`` through every tool signature, the
Orchestrator sets a :class:`SessionContext` on a :class:`contextvars.ContextVar`
around each agent run.  Tools read it via :func:`get_current`.  This mirrors the
``contextvars``-based span propagation in ``observability/trace.py``.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    """The session/channel that initiated the current agent run."""

    session_key: str
    source: str = ""  # channel: "cli" | "http" | "websocket" | "wechat" | ...


_current: ContextVar[SessionContext | None] = ContextVar(
    "mybot_session_context", default=None
)


def set_current(ctx: SessionContext) -> Token:
    """Bind *ctx* as the current session context.  Returns a reset token."""
    return _current.set(ctx)


def reset(token: Token) -> None:
    """Restore the previous session context using *token* from :func:`set_current`."""
    _current.reset(token)


def get_current() -> SessionContext | None:
    """Return the current :class:`SessionContext`, or ``None`` if unset."""
    return _current.get()
