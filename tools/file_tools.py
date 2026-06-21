"""ReadTool and WriteTool — safe file I/O within the workspace.

All paths are resolved relative to the workspace root. Path traversal
(``..``), symlinks, and absolute paths that escape the workspace are
rejected before any I/O happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .guard import Capability
from .tool import Tool, ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_READ_BYTES = 1_000_000    # 1 MB — refuse to read files larger than this
MAX_WRITE_BYTES = 500_000     # 500 KB — refuse to write content larger than this
MAX_DIR_LIST_ENTRIES = 200    # cap directory listings
_BINARY_SIGNAL = "\x00"       # null-byte check for binary files


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _resolve_safe(workspace: Path, file_path: str) -> Path | None:
    """Resolve *file_path* relative to *workspace*, rejecting escapes.

    Returns the resolved absolute ``Path``, or ``None`` when:
    - the path contains enough ``..`` to escape the workspace
    - any component of the path (or its ancestors) is a symlink
    - the resolved path is not inside the workspace
    - the path itself is malformed (OS-level)
    """
    ws = Path(workspace).resolve()

    # Reject empty paths early
    stripped = file_path.strip()
    if not stripped:
        return None

    # Check for symlinks *before* resolving, because resolve() follows them.
    # Walk each component of the relative path checking is_symlink().
    try:
        parts = Path(stripped).parts
    except (ValueError, OSError):
        return None

    current = ws
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None

    # Now resolve and verify bounds
    try:
        candidate = current.resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        return None

    # Check if workspace itself contains symlinks (walk resolved path ancestors,
    # stopping at workspace boundary)
    for parent in candidate.parents:
        try:
            parent.relative_to(ws)
        except ValueError:
            break  # outside workspace
        try:
            if parent.is_symlink():
                return None
        except OSError:
            pass

    # Must be within workspace
    try:
        candidate.relative_to(ws)
    except ValueError:
        return None

    return candidate


# ---------------------------------------------------------------------------
# ReadTool
# ---------------------------------------------------------------------------


class ReadTool(Tool):
    """Read a file or list a directory within the workspace.

    If *path* is a directory the contents are listed (sorted, dirs first).
    When *path* is a file the content is returned with 1-indexed line numbers.
    Binary files and files exceeding the size cap are rejected.
    """

    name = "read"
    _scopes = {"core", "subagent", "memory"}
    _parallel = True
    capabilities = {Capability.FILE_READ}
    description = (
        "Read a file or list a directory within the workspace. "
        "If path is a directory, entries are listed (dirs first, sorted by name). "
        "If path is a file, returns content with line numbers like 'cat -n'. "
        "Binary files and files larger than 1 MB are rejected."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file or directory to read/list.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed, default: 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return (default: all lines up to the size cap).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(
        self,
        path: str = "",
        offset: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        # Accept both new "path" and legacy "file_path" parameter names
        actual_path = path or kwargs.get("file_path", "")
        p = _resolve_safe(self.workspace, actual_path)
        if p is None:
            return ToolResult(
                success=False, content="", error=f"Invalid or unsafe path: {actual_path!r}"
            )

        # --- directory --------------------------------------------------------
        if p.is_dir():
            return self._list_dir(p)

        # --- existence & type ------------------------------------------------
        if not p.exists():
            return ToolResult(
                success=False, content="", error=f"File not found: {actual_path!r}"
            )

        if not p.is_file():
            return ToolResult(
                success=False,
                content="",
                error=f"Not a regular file: {actual_path!r}",
            )

        # --- size ------------------------------------------------------------
        try:
            stat = p.stat()
        except OSError as exc:
            return ToolResult(
                success=False, content="", error=f"Cannot stat file: {exc}"
            )

        if stat.st_size > MAX_READ_BYTES:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"File too large ({stat.st_size} > {MAX_READ_BYTES} bytes). "
                    "Use BashTool to operate on large files."
                ),
            )

        # --- read ------------------------------------------------------------
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return ToolResult(
                success=False, content="", error=f"Cannot read file: {exc}"
            )

        # Binary check — look for null bytes in the first chunk
        if _BINARY_SIGNAL in text[:8192]:
            return ToolResult(
                success=False,
                content="",
                error=f"File appears to be binary: {actual_path!r}",
            )

        # --- format output ---------------------------------------------------
        lines = text.split("\n")
        start = 0 if offset is None else max(0, offset - 1)
        end = len(lines)
        if limit is not None:
            end = min(end, start + max(0, limit))

        result = "\n".join(
            f"{i + 1}\t{lines[i]}" for i in range(start, end)
        )
        return ToolResult(success=True, content=result)

    # -- helpers ---------------------------------------------------------------

    def _list_dir(self, path: Path) -> ToolResult:
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return ToolResult(
                success=False, content="", error=f"Permission denied: {path}"
            )

        if not entries:
            return ToolResult(success=True, content="(empty directory)")

        if len(entries) > MAX_DIR_LIST_ENTRIES:
            entries = entries[:MAX_DIR_LIST_ENTRIES]
            truncated = True
        else:
            truncated = False

        lines = []
        for entry in entries:
            try:
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"  {entry.name}{suffix}")
            except OSError:
                lines.append(f"  {entry.name}")

        if truncated:
            lines.append(f"  ... ({MAX_DIR_LIST_ENTRIES} entries shown)")

        return ToolResult(success=True, content="\n".join(lines))


# ---------------------------------------------------------------------------
# WriteTool
# ---------------------------------------------------------------------------


class WriteTool(Tool):
    """Write content to a file within the workspace.

    Creates missing parent directories automatically.  Existing files are
    overwritten; directories and symlinks are rejected.
    """

    name = "write"
    _scopes = {"core", "subagent"}
    _parallel = False
    capabilities = {Capability.FILE_WRITE}
    description = (
        "Write content to a file in the workspace. Creates parent directories "
        "as needed. Overwrites existing files. Directories and symlinks are "
        "rejected. Content is capped at 500 KB."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file to write/create.",
            },
            "content": {
                "type": "string",
                "description": "The exact text content to write.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(self, path: str = "", content: str = "", **kwargs: Any) -> ToolResult:
        # Accept both new "path" and legacy "file_path" parameter names
        actual_path = path or kwargs.get("file_path", "")
        p = _resolve_safe(self.workspace, actual_path)
        if p is None:
            return ToolResult(
                success=False, content="", error=f"Invalid or unsafe path: {actual_path!r}"
            )

        if len(content) > MAX_WRITE_BYTES:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Content too large ({len(content)} > {MAX_WRITE_BYTES} bytes). "
                    "Split into multiple writes or use BashTool."
                ),
            )

        if p.is_dir():
            return ToolResult(
                success=False, content="", error=f"Path is a directory: {actual_path!r}"
            )

        # Create parent directories
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(
                success=False, content="", error=f"Cannot create parent directory: {exc}"
            )

        try:
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                success=False, content="", error=f"Cannot write file: {exc}"
            )

        return ToolResult(success=True, content=f"Wrote {len(content)} bytes to {actual_path!r}")


# ---------------------------------------------------------------------------
# ListDirTool (ls)
# ---------------------------------------------------------------------------


class ListDirTool(Tool):
    """List files and subdirectories in a directory within the workspace.

    This is the **primary tool for exploring directory contents**. Prefer this
    over running ``find`` or ``ls`` via bash — it is faster, safer, and
    respects workspace boundaries automatically.

    Entries are sorted with directories first, then by name.  Supports
    recursive listing up to a configurable depth.  Capped at 200 entries.
    """

    name = "ls"
    _scopes = {"core", "subagent", "memory"}
    _parallel = True
    capabilities = {Capability.FILE_READ}
    description = (
        "List files and subdirectories in a directory — use this instead of "
        "running 'find' or 'ls' via bash. "
        "Returns entries sorted with directories first, then alphabetically. "
        "Set recursive=true to list subdirectories recursively (depth limit 3). "
        "Capped at 200 entries. "
        "Use this whenever you need to explore a directory or find files by name pattern."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "dir_path": {
                "type": "string",
                "description": "Path to the directory to list (use '.' for the workspace root).",
            },
            "recursive": {
                "type": "boolean",
                "description": "List subdirectories recursively (default: false, max depth: 3).",
            },
            "glob": {
                "type": "string",
                "description": "Optional filename pattern, e.g. '*.py' or 'test_*.py' (default: all files).",
            },
        },
        "required": ["dir_path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(
        self,
        dir_path: str,
        recursive: bool = False,
        glob: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        p = _resolve_safe(self.workspace, dir_path)
        if p is None:
            return ToolResult(
                success=False, content="", error=f"Invalid or unsafe path: {dir_path!r}"
            )

        if not p.is_dir():
            return ToolResult(
                success=False, content="",
                error=f"Not a directory: {dir_path!r}. Use 'read' to read files.",
            )

        max_depth = 3 if recursive else 1
        return self._list_dir(p, max_depth=max_depth, glob_pattern=glob)

    def _list_dir(self, path: Path, max_depth: int = 1, glob_pattern: str = "") -> ToolResult:
        from fnmatch import fnmatch

        entries: list[Path] = []
        try:
            for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                entries.append(entry)
        except PermissionError:
            return ToolResult(
                success=False, content="", error=f"Permission denied: {path}"
            )

        if max_depth > 1:
            subdirs = [e for e in entries if e.is_dir()]
            for sub in subdirs:
                try:
                    for entry in sorted(sub.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                        entries.append(entry)
                except PermissionError:
                    pass

        if not entries:
            return ToolResult(success=True, content="(empty directory)")

        # Apply glob filter
        if glob_pattern:
            entries = [e for e in entries if fnmatch(e.name, glob_pattern)]

        if not entries:
            return ToolResult(success=True, content=f"(no entries matching {glob_pattern!r})")

        if len(entries) > MAX_DIR_LIST_ENTRIES:
            entries = entries[:MAX_DIR_LIST_ENTRIES]
            truncated = True
        else:
            truncated = False

        lines = []
        for entry in entries:
            try:
                suffix = "/" if entry.is_dir() else ""
                # Show relative path for recursive listings
                if max_depth > 1:
                    try:
                        rel = entry.relative_to(path.parent)
                        lines.append(f"  {rel}{suffix}")
                    except ValueError:
                        lines.append(f"  {entry.name}{suffix}")
                else:
                    lines.append(f"  {entry.name}{suffix}")
            except OSError:
                lines.append(f"  {entry.name}")

        if truncated:
            lines.append(f"  ... ({MAX_DIR_LIST_ENTRIES} entries shown)")

        return ToolResult(success=True, content="\n".join(lines))
