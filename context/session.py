"""Session — per-conversation message history with disk persistence."""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from observability.metrics import REGISTRY

_MAX_CACHED_SESSIONS = 128


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    consolidated_cursor: int = 0
    last_consolidated: int = 0    # index of last Consolidator-archived message
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Manages conversation sessions with JSON file persistence.

    Each session is stored as ``workspace/sessions/<key>.json``.
    """

    def __init__(
        self,
        workspace: Path,
        sessions: dict[str, Session] | None = None,
        *,
        max_cached: int = _MAX_CACHED_SESSIONS,
    ) -> None:
        self.workspace = workspace
        self.sessions: OrderedDict[str, Session] = (
            OrderedDict(sessions) if sessions else OrderedDict()
        )
        self._max_cached = max_cached
        self.sessions_dir = self.workspace / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._write_locks: dict[str, asyncio.Lock] = {}

    def _evict_lru(self) -> None:
        """Evict the least recently used session from cache (keeps disk copy)."""
        if len(self.sessions) <= self._max_cached:
            return
        evicted_key, evicted_session = self.sessions.popitem(last=False)
        REGISTRY.active_sessions.dec()
        logger.debug(
            "LRU evict session {!r} ({} msgs, cached {})",
            evicted_key, len(evicted_session.messages), len(self.sessions),
        )

    # -- retrieve ----------------------------------------------------------------

    def get_session(self, key: str) -> Session:
        """Get a session by key, loading from disk if not in memory."""
        session = self.sessions.get(key)
        if session is not None:
            self.sessions.move_to_end(key)
            return session
        session = self._load_from_path(key)
        if session is None:
            session = Session(key=key)
            REGISTRY.active_sessions.inc()
        self.sessions[key] = session
        self.sessions.move_to_end(key)
        self._evict_lru()
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

    def prune_archived_messages(self, key: str) -> int:
        """Remove messages that have been both consolidated and archived.

        Returns the number of messages pruned.  Call after consolidation
        advances ``last_consolidated`` to prevent unbounded growth of the
        in-memory message list.
        """
        session = self.sessions.get(key)
        if session is None:
            return 0
        prune_before = min(session.consolidated_cursor, session.last_consolidated)
        if prune_before <= 0:
            return 0
        removed = prune_before
        session.messages = session.messages[prune_before:]
        session.consolidated_cursor = max(0, session.consolidated_cursor - prune_before)
        session.last_consolidated = max(0, session.last_consolidated - prune_before)
        self.save_session(session)
        logger.debug(
            "Pruned {} archived messages from session {!r} ({} remaining)",
            removed, key, len(session.messages),
        )
        return removed

    # -- concurrency -------------------------------------------------------------

    def _get_write_lock(self, key: str) -> asyncio.Lock:
        """Return the per-session write lock, creating it lazily."""
        if key not in self._write_locks:
            self._write_locks[key] = asyncio.Lock()
        return self._write_locks[key]

    @asynccontextmanager
    async def lock_session(self, key: str):
        """Async context manager that holds the write lock for *key*.

        Usage::

            async with session_mgr.lock_session(key):
                session = session_mgr.get_session(key)
                session.messages.append(msg)
                session_mgr.save_session(session)
        """
        lock = self._get_write_lock(key)
        async with lock:
            yield

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
        removed = self.sessions.pop(key, None)
        if removed is not None:
            REGISTRY.active_sessions.dec()

    def delete_session(self, key: str) -> bool:
        """Delete a session from memory and disk. Returns True if deleted."""
        removed = self.sessions.pop(key, None)
        if removed is not None:
            REGISTRY.active_sessions.dec()
        path = self.sessions_dir / f"{key}.json"
        if path.exists():
            path.unlink()
            logger.debug("Session {!r} deleted", key)
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all session files with metadata.

        Files that parse as a dict but lack a top-level ``"key"`` are not
        sessions this manager wrote (every :meth:`save_session` includes
        ``"key"``) — e.g. runner checkpoints that share the directory — so
        they are skipped rather than surfaced with a fabricated key.
        """
        result: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    result.append({"key": path.stem, "message_count": 0,
                                   "error": f"unexpected type: {type(data).__name__}"})
                    continue
                if "key" not in data:
                    continue  # not a session (e.g. a runner checkpoint)
                result.append({
                    "key": data["key"],
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                })
            except (json.JSONDecodeError, OSError):
                result.append({"key": path.stem, "message_count": 0, "error": "unreadable"})
        return result
