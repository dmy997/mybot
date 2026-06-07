"""Memory system data types and constants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Memory types from Claude Code memory conventions
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# File names
_MEMORY_INDEX = "MEMORY.md"
_HISTORY_FILE = "history.jsonl"
_CURSOR_FILE = ".cursor"
_DREAM_CURSOR_FILE = ".dream_cursor"
_SOUL_FILE = "SOUL.md"
_USER_FILE = "USER.md"

# Frontmatter delimiters
_FM_DELIM = "---"
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*", re.DOTALL)
_NAMESPACE_KEYS = frozenset({"metadata"})

# Link pattern: [[name]]
_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass
class MemoryEntry:
    """A single memory entry."""

    name: str
    type: str  # user | feedback | project | reference
    description: str
    content: str  # markdown body without frontmatter

    # Derived — set when reading from disk
    file_path: str | None = None

    def to_frontmatter_text(self) -> str:
        """Render the full markdown file content with frontmatter."""
        fm = (
            f"---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"metadata:\n"
            f"  type: {self.type}\n"
            f"---"
        )
        content = self.content.strip()
        if content:
            return f"{fm}\n\n{content}\n"
        return f"{fm}\n"

    @classmethod
    def from_frontmatter_text(cls, text: str) -> MemoryEntry | None:
        """Parse a markdown file with frontmatter into a MemoryEntry."""
        fm_data, body = parse_frontmatter(text)
        if not fm_data:
            return None
        name = fm_data.get("name", "")
        description = fm_data.get("description", "")
        metadata = fm_data.get("metadata", {})
        if isinstance(metadata, dict):
            mem_type = metadata.get("type", "user")
        else:
            mem_type = "user"
        if not name:
            return None
        return cls(
            name=name,
            type=mem_type,
            description=description,
            content=body.strip(),
        )

    @property
    def relative_path(self) -> str:
        """Relative path from memory dir: 'user/user-role.md'."""
        return f"{self.type}/{self.name}.md"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like frontmatter from markdown text.

    Returns (frontmatter_dict, body_text).
    Handles simple key: value pairs and 2-space nested metadata.
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text

    fm_block = m.group(1)
    body = text[m.end():]
    result: dict[str, Any] = {}
    current_ns: dict[str, Any] | None = None
    current_ns_indent: int = 0

    for line in fm_block.split("\n"):
        stripped = line.rstrip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip())
        kv = stripped.split(":", 1)
        if len(kv) != 2:
            continue

        key = kv[0].strip()
        value = kv[1].strip()

        # Detect nested block — only for known namespace keys
        if not value and key in _NAMESPACE_KEYS:
            current_ns = {}
            current_ns_indent = indent
            result[key] = current_ns
            continue

        # Empty value on a non-namespace key → empty string
        if not value:
            current_ns = None
            result[key] = ""
            continue

        # Nested value (indented under a namespace)
        if current_ns is not None and indent > current_ns_indent:
            current_ns[key] = _cast_value(value)
            continue

        current_ns = None
        result[key] = _cast_value(value)

    return result, body


def _cast_value(value: str) -> Any:
    """Cast string value to appropriate type."""
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "None", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    # Unquote
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def extract_links(content: str) -> list[str]:
    """Extract [[name]] references from content."""
    return _LINK_PATTERN.findall(content)


def format_memory_index(entries: list[MemoryEntry]) -> str:
    """Render MEMORY.md from a list of entries."""
    lines: list[str] = []
    for entry in entries:
        lines.append(f"- [{entry.name}]({entry.relative_path}) — {entry.description}")
    return "\n".join(lines) + "\n"


def parse_memory_index_line(line: str) -> tuple[str, str, str] | None:
    """Parse a MEMORY.md index line like '- [Title](path) — desc'.

    Returns (name, path, description) or None.
    """
    m = re.match(r"^-\s+\[([^\]]+)\]\(([^)]+)\)\s*[—\-]\s*(.+)$", line)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).strip()
