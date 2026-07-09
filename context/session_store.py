"""SessionStore — owns SessionManager, handles session persistence lifecycle.

Extracted from ContextManager so session persistence can be tested and
reasoned about independently of context assembly and compression.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .session import SessionManager


class SessionStore:
    """Owns :class:`SessionManager` and provides session persistence operations."""

    def __init__(
        self,
        workspace: Path,
        max_session_messages: int = 2000,
        session_ttl_days: int = 30,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._max_session_messages = max_session_messages
        self._session_ttl_days = session_ttl_days
        self.session = SessionManager(self.workspace)

    # -- persistence ----------------------------------------------------------

    async def save_exchange(
        self,
        session_key: str,
        user_input: str,
        assistant_messages: list[dict[str, Any]],
        *,
        tools_used: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Append a user+assistant exchange to the session log."""
        async with self.session.lock_session(session_key):
            session = self.session.get_session(session_key)
            session.messages.append({"role": "user", "content": user_input})
            for msg in assistant_messages:
                session.messages.append(msg)
            session.updated_at = datetime.now()
            self.session.save_session(session)
            self.session.prune_by_count(session_key, self._max_session_messages)

    async def save_session(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist the full message list after an agent run."""
        async with self.session.lock_session(session_key):
            self.session.set_messages(session_key, messages)

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

    def purge_expired_sessions(self) -> int:
        """Delete sessions whose last update exceeds ``session_ttl_days``."""
        return self.session.purge_expired_sessions(self._session_ttl_days)
