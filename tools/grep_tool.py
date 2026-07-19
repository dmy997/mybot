"""GrepTool — fast regex code search within the workspace."""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .guard import Capability
from .tool import Tool, ToolResult

MAX_MATCHES = 100
MAX_FILE_SIZE = 1_000_000
MAX_LINE_LENGTH = 500

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".tox", ".eggs",
    "dist", "build", ".mypy_cache", ".claude",
})

_SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".o", ".a", ".obj",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3",
})


class GrepTool(Tool):
    """Search files in the workspace with regex pattern matching.

    Returns matching lines with file paths and line numbers.
    Binary files and common VCS/build directories are skipped.
    """

    name = "grep"
    _scopes = {"core", "subagent", "memory"}
    _parallel = True
    capabilities = {Capability.FILE_READ}
    description = (
        "Search for a regex pattern across workspace files. "
        "Use for: finding where a function/class/variable is defined or used, "
        "locating error messages in logs, discovering relevant code patterns. "
        "NOT for: reading a single known file (use read), exploring directory "
        "structure (use ls), or full-text web search (use websearch). "
        "Returns matching lines with file paths and line numbers. "
        "Binary files and common VCS/build directories are automatically skipped."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regex pattern to search for (Python re syntax).",
            },
            "path": {
                "type": "string",
                "description": "Subdirectory or file to search within (default: entire workspace).",
            },
            "glob": {
                "type": "string",
                "description": "File pattern filter, e.g. '*.py' (default: all text files).",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false).",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def execute(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        **_: Any,
    ) -> ToolResult:
        if not pattern or not pattern.strip():
            return ToolResult(success=False, content="", error="pattern must not be empty")

        try:
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(success=False, content="", error=f"Invalid regex: {exc}")

        search_root = self.workspace
        if path:
            p = (search_root / path).resolve()
            try:
                p.relative_to(search_root)
            except ValueError:
                return ToolResult(
                    success=False, content="", error=f"Path outside workspace: {path!r}"
                )
            if not p.exists():
                return ToolResult(
                    success=False, content="", error=f"Path not found: {path!r}"
                )
            search_root = p

        results: list[str] = []
        match_count = 0

        try:
            if search_root.is_file():
                for m in self._search_file(search_root, regex):
                    results.append(m)
            else:
                for file_path in search_root.rglob("*"):
                    if match_count >= MAX_MATCHES:
                        break
                    if not file_path.is_file():
                        continue
                    if self._should_skip(file_path, glob):
                        continue
                    for m in self._search_file(file_path, regex):
                        if match_count >= MAX_MATCHES:
                            results.append("... (truncated, reached max matches)")
                            break
                        results.append(m)
                        match_count += 1
        except PermissionError:
            pass

        if not results:
            return ToolResult(success=True, content="No matches found.")

        return ToolResult(success=True, content="\n".join(results))

    # -- helpers ---------------------------------------------------------------

    def _should_skip(self, file_path: Path, glob_pattern: str | None) -> bool:
        """Check whether *file_path* should be skipped."""
        # Skip hidden files/dirs (except .env.example)
        for part in file_path.parts:
            if part.startswith(".") and part not in (".env.example",):
                return True

        # Skip known directories
        if any(d in _SKIP_DIRS for d in file_path.parts):
            return True

        # Skip binary extensions
        if file_path.suffix.lower() in _SKIP_EXTENSIONS:
            return True

        # Size check
        try:
            if file_path.stat().st_size > MAX_FILE_SIZE:
                return True
        except OSError:
            return True

        # Glob filter
        if glob_pattern and not fnmatch(file_path.name, glob_pattern):
            return True

        return False

    def _search_file(self, file_path: Path, regex: re.Pattern) -> list[str]:
        """Search a single file for regex matches."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        rel_path = file_path.relative_to(self.workspace)
        results: list[str] = []
        for line_no, line in enumerate(content.split("\n"), 1):
            if regex.search(line):
                line_display = line[:MAX_LINE_LENGTH]
                if len(line) > MAX_LINE_LENGTH:
                    line_display += "..."
                results.append(f"{rel_path}:{line_no}: {line_display}")
        return results
