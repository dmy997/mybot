"""MemoryStore — pure file I/O for the agent memory system.

Mirrors nanobot's MemoryStore architecture with adaptations for
Claude Code-compatible memory file conventions.
"""

from __future__ import annotations

from pathlib import Path

from .types import (
    _MEMORY_INDEX,
    _SOUL_FILE,
    _USER_FILE,
    MEMORY_TYPES,
    MemoryEntry,
    format_memory_index,
    parse_memory_index_line,
)


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, individual .md files.

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
            └── reference/
                └── <name>.md
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory_dir = self.workspace / "memory"
        self._index_file = self.memory_dir / _MEMORY_INDEX
        self._soul_file = self.workspace / _SOUL_FILE
        self._user_file = self.workspace / _USER_FILE
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

