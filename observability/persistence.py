"""Per-session observability persistence to JSONL files.

Stores log events and completed spans to ``{workspace}/observability/{session_key}.jsonl``.
Append-only writes with per-file threading locks for safety.

Usage::

    from observability.persistence import init_store, store

    # Called once at startup by Orchestrator:
    init_store(Path("~/.mybot/workspace"))

    # Called by emit() and tracer callbacks:
    if store is not None:
        store.save_event(session_key, event_type, data)
        store.save_span(session_key, span_entry)
"""

from __future__ import annotations

import json
import threading
import time as _time_mod
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level singleton (initialized by Orchestrator)
# ---------------------------------------------------------------------------

store: ObservabilityStore | None = None


def init_store(workspace: Path) -> ObservabilityStore:
    global store
    store = ObservabilityStore(workspace)
    return store


# ---------------------------------------------------------------------------
# ObservabilityStore
# ---------------------------------------------------------------------------


class ObservabilityStore:
    """Thread-safe per-session JSONL persistence for observability data."""

    _MAX_EVENTS = 2000
    _MAX_SPANS = 1000
    _TRIM_INTERVAL = 50  # writes between auto-trim checks
    _MAX_SESSION_FILES = 200

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace).expanduser().resolve() / "observability"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._write_counts: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------------

    def _get_lock(self, session_key: str) -> threading.Lock:
        with self._locks_lock:
            if session_key not in self._locks:
                self._locks[session_key] = threading.Lock()
            return self._locks[session_key]

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.jsonl"

    # -- write -----------------------------------------------------------------

    def save_event(self, session_key: str, event_type: str, data: dict[str, Any]) -> None:
        """Append a structured log event to the session JSONL file."""
        line = json.dumps({
            "type": "event",
            "session_key": session_key,
            "event_type": event_type,
            "timestamp": _time_mod.time(),
            "data": data,
        }, ensure_ascii=False, default=str)

        with self._get_lock(session_key):
            with open(self._path(session_key), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        self._maybe_trim(session_key)

    def save_span(self, session_key: str, span_entry: dict[str, Any]) -> None:
        """Append a completed span to the session JSONL file."""
        line = json.dumps({
            "type": "span",
            "session_key": session_key,
            **span_entry,
        }, ensure_ascii=False, default=str)

        with self._get_lock(session_key):
            with open(self._path(session_key), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        self._maybe_trim(session_key)

    # -- auto-trim -------------------------------------------------------------

    def _maybe_trim(self, session_key: str) -> None:
        self._write_counts[session_key] = self._write_counts.get(session_key, 0) + 1
        if self._write_counts[session_key] >= self._TRIM_INTERVAL:
            self.trim(session_key, self._MAX_EVENTS, self._MAX_SPANS)
            self._write_counts[session_key] = 0

    def cleanup_stale_files(self, active_sessions: set[str] | None = None) -> int:
        """Remove JSONL files for sessions that no longer exist, and cap total files.

        Returns number of files removed.
        """
        removed = 0
        jsonl_files = sorted(
            [f for f in self._dir.glob("*.jsonl") if not f.stem.startswith("_")],
            key=lambda p: p.stat().st_mtime,
        )

        # Phase 1: remove files for sessions that no longer exist
        if active_sessions is not None:
            for f in jsonl_files[:]:
                if f.stem not in active_sessions:
                    f.unlink(missing_ok=True)
                    jsonl_files.remove(f)
                    removed += 1

        # Phase 2: cap total file count, oldest first
        while len(jsonl_files) > self._MAX_SESSION_FILES:
            jsonl_files[0].unlink(missing_ok=True)
            jsonl_files.pop(0)
            removed += 1

        return removed

    # -- read ------------------------------------------------------------------

    def load_events(self, session_key: str, limit: int = 200) -> list[dict[str, Any]]:
        """Load recent log events for *session_key*, newest first."""
        return self._load_by_type(session_key, "event", limit)

    def load_spans(self, session_key: str, limit: int = 200) -> list[dict[str, Any]]:
        """Load recent completed spans for *session_key*, newest first."""
        return self._load_by_type(session_key, "span", limit)

    def _load_by_type(self, session_key: str, kind: str, limit: int) -> list[dict[str, Any]]:
        path = self._path(session_key)
        if not path.exists():
            return []

        results: list[dict[str, Any]] = []
        with self._get_lock(session_key):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == kind:
                        results.append(obj)
                        if len(results) > limit * 2:
                            results = results[-limit:]

        return list(reversed(results[-limit:]))

    # -- list sessions ---------------------------------------------------------

    def list_sessions(self) -> list[str]:
        """Return session keys that have observability data, newest first."""
        entries: list[tuple[str, float]] = []
        for f in self._dir.glob("*.jsonl"):
            key = f.stem
            mtime = f.stat().st_mtime
            entries.append((key, mtime))
        entries.sort(key=lambda x: x[1], reverse=True)
        return [e[0] for e in entries]

    # -- trim ------------------------------------------------------------------

    def _metrics_path(self) -> Path:
        return self._dir / "_metrics.json"

    def save_metrics(self, data: dict[str, Any]) -> None:
        """Persist a metrics snapshot to disk (atomic write)."""
        path = self._metrics_path()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        tmp.replace(path)

    def load_metrics(self) -> dict[str, Any] | None:
        """Load the last persisted metrics snapshot, or None."""
        path = self._metrics_path()
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def load_all_events(self, limit: int = 200) -> list[dict[str, Any]]:
        """Load recent events across ALL sessions, newest first."""
        return self._load_all_by_type("event", limit)

    def load_all_spans(self, limit: int = 200) -> list[dict[str, Any]]:
        """Load recent spans across ALL sessions, newest first."""
        return self._load_all_by_type("span", limit)

    def _load_all_by_type(self, kind: str, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(
            self._dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if path.stem.startswith("_"):
                continue
            results.extend(self._load_by_type(path.stem, kind, limit))
        results.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return results[:limit]

    def trim(self, session_key: str, max_events: int = 2000, max_spans: int = 1000) -> None:
        """Trim old entries from a session file, keeping the most recent of each type."""
        path = self._path(session_key)
        if not path.exists():
            return

        with self._get_lock(session_key):
            events: list[str] = []
            spans: list[str] = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "event":
                        events.append(line)
                        if len(events) > max_events:
                            events = events[-max_events:]
                    elif obj.get("type") == "span":
                        spans.append(line)
                        if len(spans) > max_spans:
                            spans = spans[-max_spans:]

            combined = events + spans
            with open(path, "w", encoding="utf-8") as f:
                for line in combined:
                    f.write(line + "\n")
