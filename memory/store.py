"""MemoryStore — pure file I/O for the agent memory system.

Mirrors nanobot's MemoryStore architecture with adaptations for
Claude Code-compatible memory file conventions.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from .types import (
    _CURSOR_FILE,
    _DREAM_CURSOR_FILE,
    _HISTORY_FILE,
    _MEMORY_INDEX,
    _SOUL_FILE,
    _USER_FILE,
    MEMORY_TYPES,
    MemoryEntry,
    format_memory_index,
    parse_memory_index_line,
)


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, individual .md files, history.jsonl.

    Directory layout::

        workspace/
        ├── SOUL.md
        ├── USER.md
        └── memory/
            ├── MEMORY.md              # index of all memories
            ├── user/                  # type-specific subdirs
            │   └── <name>.md
            ├── feedback/
            │   └── <name>.md
            ├── project/
            │   └── <name>.md
            ├── reference/
            │   └── <name>.md
            ├── history.jsonl          # append-only conversation log
            ├── .cursor                # last history cursor
            └── .dream_cursor          # last dream cursor (reserved)
    """

    _DEFAULT_MAX_HISTORY = 1000

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = Path(workspace).expanduser().resolve()
        self.max_history_entries = max_history_entries
        self.memory_dir = self.workspace / "memory"
        self._index_file = self.memory_dir / _MEMORY_INDEX
        self._history_file = self.memory_dir / _HISTORY_FILE
        self._cursor_file = self.memory_dir / _CURSOR_FILE
        self._dream_cursor_file = self.memory_dir / _DREAM_CURSOR_FILE
        self._soul_file = self.workspace / _SOUL_FILE
        self._user_file = self.workspace / _USER_FILE
        self._oversize_logged = False
        self._ensure_dirs()

    # -- init helpers ----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create memory directory and type subdirectories."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        for mtype in MEMORY_TYPES:
            (self.memory_dir / mtype).mkdir(exist_ok=True)

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

    # -- MEMORY.md index -------------------------------------------------------

    def read_memory_index(self) -> str:
        """Read the MEMORY.md index file."""
        return self._read_file(self._index_file)

    def write_memory_index(self, content: str) -> None:
        """Overwrite MEMORY.md."""
        self._write_file(self._index_file, content)

    def parse_index_entries(self) -> list[dict[str, str]]:
        """Parse MEMORY.md into a list of {name, path, description} dicts."""
        content = self.read_memory_index()
        entries: list[dict[str, str]] = []
        for line in content.splitlines():
            parsed = parse_memory_index_line(line)
            if parsed:
                name, path, desc = parsed
                entries.append({"name": name, "path": path, "description": desc})
        return entries

    def rebuild_index(self) -> str:
        """Rebuild MEMORY.md from individual memory files on disk.

        Scans all type subdirectories and regenerates the index.
        """
        entries = self._scan_memory_files()
        content = format_memory_index(entries)
        self.write_memory_index(content)
        return content

    # -- individual memory files (.md) -----------------------------------------

    def read_memory(self, name: str) -> MemoryEntry | None:
        """Read a memory entry by name. Scans all type dirs."""
        for mtype in MEMORY_TYPES:
            file_path = self.memory_dir / mtype / f"{name}.md"
            if file_path.exists():
                text = self._read_file(file_path)
                entry = MemoryEntry.from_frontmatter_text(text)
                if entry:
                    entry.file_path = str(file_path.relative_to(self.memory_dir))
                    return entry
        return None

    def write_memory(self, entry: MemoryEntry) -> Path:
        """Write a memory entry to its type subdirectory and update MEMORY.md.

        Returns the absolute path to the written file.
        """
        file_path = self.memory_dir / entry.relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_file(file_path, entry.to_frontmatter_text())

        # Update MEMORY.md index
        self._upsert_index(entry)
        return file_path

    def delete_memory(self, name: str) -> bool:
        """Delete a memory entry by name. Returns True if deleted."""
        entry = self.read_memory(name)
        if entry is None:
            return False
        file_path = self.memory_dir / entry.relative_path
        if file_path.exists():
            file_path.unlink()
        self._remove_from_index(name)
        return True

    def list_memories(self) -> list[MemoryEntry]:
        """List all memory entries from disk (not from index)."""
        return self._scan_memory_files()

    def find_memory(self, name: str) -> MemoryEntry | None:
        """Find a memory by name (alias for read_memory)."""
        return self.read_memory(name)

    def _scan_memory_files(self) -> list[MemoryEntry]:
        """Scan all type subdirectories and parse memory files."""
        entries: list[MemoryEntry] = []
        for mtype in MEMORY_TYPES:
            type_dir = self.memory_dir / mtype
            if not type_dir.is_dir():
                continue
            for file_path in sorted(type_dir.glob("*.md")):
                text = self._read_file(file_path)
                entry = MemoryEntry.from_frontmatter_text(text)
                if entry:
                    entry.file_path = str(file_path.relative_to(self.memory_dir))
                    entries.append(entry)
        return entries

    def check_reverse_sync(self) -> list[str]:
        """Check for markdown files modified externally (newer than index).

        Returns list of memory names that need re-indexing.
        """
        try:
            index_mtime = self._index_file.stat().st_mtime if self._index_file.exists() else 0
        except OSError:
            index_mtime = 0

        changed: list[str] = []
        for entry in self._scan_memory_files():
            if entry.file_path:
                fp = self.memory_dir / entry.file_path
                try:
                    if fp.stat().st_mtime > index_mtime + 1.0:  # 1s tolerance
                        changed.append(entry.name)
                except OSError:
                    pass
        return changed

    # -- index helpers ---------------------------------------------------------

    def _upsert_index(self, entry: MemoryEntry) -> None:
        """Add or update an entry in MEMORY.md."""
        lines = self.read_memory_index().splitlines()
        new_line = f"- [{entry.name}]({entry.relative_path}) — {entry.description}"
        replaced = False
        for i, line in enumerate(lines):
            parsed = parse_memory_index_line(line)
            if parsed and parsed[0] == entry.name:
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        self.write_memory_index("\n".join(lines) + "\n")

    def _remove_from_index(self, name: str) -> None:
        """Remove an entry from MEMORY.md."""
        lines = self.read_memory_index().splitlines()
        lines = [
            line for line in lines
            if not (parse_memory_index_line(line)
                    and parse_memory_index_line(line)[0] == name)
        ]
        self.write_memory_index("\n".join(lines) + "\n")

    # -- memory context for system prompt --------------------------------------

    def get_memory_context(self) -> str:
        """Build memory context text for injection into system prompt.

        Includes the full MEMORY.md content, referencing individual files.
        """
        index = self.read_memory_index()
        if not index.strip():
            return ""

        # Include full content of each memory file
        parts: list[str] = []
        for entry in self.list_memories():
            parts.append(f"## {entry.name}\n\n{entry.content}")

        index_section = f"## Memory Index\n\n{index}"
        if parts:
            return index_section + "\n" + "\n\n".join(parts)
        return index_section

    # -- history.jsonl — append-only, JSONL ------------------------------------

    def append_history(self, content: str, *, max_chars: int | None = None) -> int:
        """Append an entry to history.jsonl and return its cursor.

        Args:
            content: The history entry text.
            max_chars: Optional character cap (default 16000).
        """
        limit = max_chars if max_chars is not None else 16_000
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = content.rstrip()
        if len(text) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating",
                    limit, len(text),
                )
            text = text[:limit] + "\n... (truncated)"

        record = {"cursor": cursor, "timestamp": ts, "content": text}
        with open(self._history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with cursor > since_cursor."""
        result: list[dict[str, Any]] = []
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            try:
                cursor = int(raw)
            except (TypeError, ValueError):
                continue
            if cursor > since_cursor:
                result.append(entry)
        return result

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds max_history_entries."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    def get_last_cursor(self) -> int:
        """Return the last written history cursor."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip())
        last = self._read_last_entry()
        if last:
            with suppress(ValueError, TypeError):
                return int(last.get("cursor", 0))
        return 0

    # -- dream cursor (reserved) -----------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- JSONL helpers ---------------------------------------------------------

    def _next_cursor(self) -> int:
        """Return the next cursor value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        last = self._read_last_entry()
        if last:
            with suppress(ValueError, TypeError):
                return int(last.get("cursor", 0)) + 1
        return 1

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self._history_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Efficiently read the last entry from history.jsonl."""
        try:
            with open(self._history_file, "rb") as f:
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

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self._history_file.with_suffix(self._history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._history_file)
            with suppress(PermissionError):
                fd = os.open(str(self._history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
