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
    """Read a file (or list a directory) within the workspace.

    Returns content with 1-indexed line numbers.  Directories are listed
    rather than read.  Binary files and files exceeding the size cap are
    rejected.
    """

    name = "read"
    _scopes = {"core", "subagent", "memory"}
    _parallel = True
    capabilities = {Capability.FILE_READ}
    description = (
        "Read a file from the workspace. Returns file contents with line "
        "numbers like 'cat -n'. Directories are listed. Binary files and "
        "files larger than 1 MB are rejected."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative path to the file/directory within the workspace.",
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
        "required": ["file_path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(
        self,
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
        **_: Any,
    ) -> ToolResult:
        p = _resolve_safe(self.workspace, file_path)
        if p is None:
            return ToolResult(
                success=False, content="", error=f"Invalid or unsafe path: {file_path!r}"
            )

        # --- directory --------------------------------------------------------
        if p.is_dir():
            return self._list_dir(p)

        # --- existence & type ------------------------------------------------
        if not p.exists():
            return ToolResult(
                success=False, content="", error=f"File not found: {file_path!r}"
            )

        if not p.is_file():
            return ToolResult(
                success=False,
                content="",
                error=f"Not a regular file: {file_path!r}",
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
                error=f"File appears to be binary: {file_path!r}",
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
            "file_path": {
                "type": "string",
                "description": "Relative path to the file to write/create.",
            },
            "content": {
                "type": "string",
                "description": "The exact text content to write.",
            },
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(self, file_path: str, content: str, **_: Any) -> ToolResult:
        p = _resolve_safe(self.workspace, file_path)
        if p is None:
            return ToolResult(
                success=False, content="", error=f"Invalid or unsafe path: {file_path!r}"
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
                success=False, content="", error=f"Path is a directory: {file_path!r}"
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

        return ToolResult(success=True, content=f"Wrote {len(content)} bytes to {file_path!r}")
