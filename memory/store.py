"""MemoryStore — pure file I/O for the agent memory system.

Manages SOUL.md, USER.md, MEMORY.md (long-term knowledge), and
history.jsonl (append-only conversation summaries with cursor tracking).
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from loguru import logger

from .types import _SOUL_FILE, _USER_FILE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_HISTORY = 1000
_RAW_ARCHIVE_MAX_CHARS = 16_000
_HISTORY_ENTRY_HARD_CAP = 64_000


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md.

    Directory layout::

        workspace/
        ├── SOUL.md
        ├── USER.md
        └── memory/
            ├── MEMORY.md              # long-term knowledge (edited by Dream)
            ├── history.jsonl          # append-only conversation summaries
            ├── .cursor                # Consolidator write cursor (monotonic int)
            └── .dream_cursor          # Dream consumption cursor
    """

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = Path(workspace).expanduser().resolve()
        self.max_history_entries = max_history_entries
        self.memory_dir = self.workspace / "memory"
        # Core files
        self.memory_file = self.memory_dir / "MEMORY.md"
        self._soul_file = self.workspace / _SOUL_FILE
        self._user_file = self.workspace / _USER_FILE
        # History
        self.history_file = self.memory_dir / "history.jsonl"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._dream_date_file = self.memory_dir / ".dream_date"
        # Rate-limit warnings
        self._corruption_logged = False
        self._oversize_logged = False
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # -- generic helpers -------------------------------------------------------

    @staticmethod
    def _read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _write_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # -- SOUL.md / USER.md ----------------------------------------------------

    def read_soul(self) -> str:
        return self._read_file(self._soul_file)

    def write_soul(self, content: str) -> None:
        self._write_file(self._soul_file, content)

    def read_user(self) -> str:
        return self._read_file(self._user_file)

    def write_user(self, content: str) -> None:
        self._write_file(self._user_file, content)

    # ==========================================================================
    # MEMORY.md — long-term knowledge file (full-text, edited by Dream)
    # ==========================================================================

    def read_memory_file(self) -> str:
        """Read the full MEMORY.md content (long-term knowledge)."""
        return self._read_file(self.memory_file)

    def write_memory_file(self, content: str) -> None:
        """Overwrite MEMORY.md (used by Dream Phase 2)."""
        tmp_path = self.memory_file.with_suffix(".md.tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(self.memory_file)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def get_memory_context(self) -> str:
        """Return MEMORY.md content formatted for system-prompt injection.

        Returns empty string if the file is still a template placeholder.
        """
        content = self.read_memory_file()
        if not content.strip() or self._is_template_content(content):
            return ""
        return f"## Long-term Memory\n\n{content}"

    def _is_template_content(self, content: str) -> bool:
        """Return True if *content* looks like an unfilled template."""
        markers = [
            "Edit this file to customize",
            "(your timezone",
            "(your role",
            "(preferred language",
        ]
        lower = content.lower()
        return any(m.lower() in lower for m in markers)

    # ==========================================================================
    # history.jsonl — append-only JSONL, cursor-tracked
    # ==========================================================================

    def append_history(self, entry: str, *, max_chars: int | None = None,
                       session_key: str = "") -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net.
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = entry.rstrip()
        if len(content) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Further occurrences suppressed.",
                    limit, len(content),
                )
            content = content[:limit]
        record = {"cursor": cursor, "timestamp": ts, "content": content}
        if session_key:
            record["session_key"] = session_key
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_history(self, since_cursor: int = 0) -> list[dict]:
        """Return history entries with cursor > *since_cursor*."""
        entries = self._read_entries()
        return [e for e in entries if isinstance(e.get("cursor"), int) and e["cursor"] > since_cursor]

    def compact_history(self) -> int:
        """Drop oldest entries if the file exceeds *max_history_entries*.

        Returns the number of entries removed.
        """
        if self.max_history_entries <= 0:
            return 0
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return 0
        removed = len(entries) - self.max_history_entries
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)
        return removed

    def raw_archive(self, messages: list[dict], session_key: str = "") -> None:
        """Fallback: dump raw messages when LLM summarization fails."""
        text = self._format_messages(messages)
        if len(text) > _RAW_ARCHIVE_MAX_CHARS:
            text = text[:_RAW_ARCHIVE_MAX_CHARS]
        self.append_history(
            f"[RAW] {len(messages)} messages\n{text}",
            max_chars=_RAW_ARCHIVE_MAX_CHARS + 500,
            session_key=session_key,
        )
        logger.warning("Consolidation degraded: raw-archived {} messages", len(messages))

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for msg in messages:
            if not msg.get("content"):
                continue
            role = msg.get("role", "?").upper()
            content = str(msg["content"])
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    # -- cursor management ------------------------------------------------------

    def get_cursor(self) -> int:
        """Return the current Consolidator write cursor (0 if none)."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def get_dream_cursor(self) -> int:
        """Return the current Dream consumption cursor (0 if none)."""
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_dream_cursor(self, cursor: int) -> None:
        """Advance the Dream consumption cursor."""
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    def get_dream_date(self) -> str:
        """Return the ISO date of the last Dream run (empty string if none)."""
        return self._read_file(self._dream_date_file).strip()

    def set_dream_date(self, iso_date: str) -> None:
        """Record the ISO date when Dream last ran."""
        self._write_file(self._dream_date_file, iso_date)

    # -- JSONL helpers ---------------------------------------------------------

    def _read_entries(self) -> list[dict]:
        """Read all entries from history.jsonl."""
        entries: list[dict] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return entries

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        last = self._read_last_entry()
        if last and isinstance(last.get("cursor"), int):
            return last["cursor"] + 1
        entries = self._read_entries()
        max_cursor = max(
            (e["cursor"] for e in entries if isinstance(e.get("cursor"), int)),
            default=0,
        )
        return max_cursor + 1

    def _read_last_entry(self) -> dict | None:
        """Read the last entry from history.jsonl efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(".jsonl.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

