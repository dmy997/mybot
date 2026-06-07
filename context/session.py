"""Session — per-conversation message history with disk persistence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    consolidated_cursor: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Manages conversation sessions with JSON file persistence.

    Each session is stored as ``workspace/sessions/<key>.json``.
    """

    def __init__(
        self,
        workspace: Path,
        sessions: dict[str, Session] | None = None,
    ) -> None:
        self.workspace = workspace
        self.sessions = sessions or {}
        self.sessions_dir = self.workspace / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # -- retrieve ----------------------------------------------------------------

    def get_session(self, key: str) -> Session:
        """Get a session by key, loading from disk if not in memory."""
        session = self.sessions.get(key)
        if session is not None:
            return session
        session = self._load_from_path(key)
        if session is None:
            session = Session(key=key)
        self.sessions[key] = session
        return session

    def get_session_history(self, key: str) -> list[dict[str, Any]]:
        """Return the message list for *key*."""
        return list(self.get_session(key).messages)

    # -- mutate ------------------------------------------------------------------

    def add_message_to_session(self, key: str, message: dict[str, Any]) -> None:
        """Append a message to the session and persist."""
        session = self.get_session(key)
        session.messages.append(message)
        session.updated_at = datetime.now()
        self.save_session(session)

    def add_messages_to_session(self, key: str, messages: list[dict[str, Any]]) -> None:
        """Append multiple messages to the session and persist."""
        session = self.get_session(key)
        session.messages.extend(messages)
        session.updated_at = datetime.now()
        self.save_session(session)

    def set_messages(self, key: str, messages: list[dict[str, Any]]) -> None:
        """Replace the entire message list for *key*."""
        session = self.get_session(key)
        session.messages = list(messages)
        session.updated_at = datetime.now()
        self.save_session(session)

    def set_consolidated_cursor(self, key: str, cursor: int) -> None:
        """Mark messages up to *cursor* as consolidated (summarised)."""
        session = self.get_session(key)
        session.consolidated_cursor = cursor
        self.save_session(session)

    # -- persistence -------------------------------------------------------------

    def save_session(self, session: Session, *, fsync: bool = False) -> None:
        """Persist *session* to disk as JSON."""
        path = self.sessions_dir / f"{session.key}.json"
        data = {
            "key": session.key,
            "messages": session.messages,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "consolidated_cursor": session.consolidated_cursor,
            "metadata": session.metadata,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        if fsync:
            with open(tmp, "ab") as f:
                os.fsync(f.fileno())
        os.replace(tmp, path)
        logger.debug("Session {!r} saved ({} messages)", session.key, len(session.messages))

    def _load_from_path(self, key: str) -> Session | None:
        """Load a session from its JSON file, or None."""
        path = self.sessions_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Session(
                key=data["key"],
                messages=data.get("messages", []),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data.get("updated_at", data["created_at"])),
                consolidated_cursor=data.get("consolidated_cursor", 0),
                metadata=data.get("metadata", {}),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load session {!r}: {}", key, exc)
            return None

    # -- lifecycle ---------------------------------------------------------------

    def remove_session(self, key: str) -> None:
        """Remove from memory only (keep on disk)."""
        self.sessions.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """Delete a session from memory and disk. Returns True if deleted."""
        self.sessions.pop(key, None)
        path = self.sessions_dir / f"{key}.json"
        if path.exists():
            path.unlink()
            logger.debug("Session {!r} deleted", key)
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all session files with metadata."""
        result: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append({
                    "key": data.get("key", path.stem),
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                })
            except (json.JSONDecodeError, OSError):
                result.append({"key": path.stem, "message_count": 0, "error": "unreadable"})
        return result
