"""BashTool — execute shell commands with sandbox restrictions.

Commands are validated against a blocklist of dangerous patterns, run with a
timeout, and their output is capped to prevent context pollution.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from .guard import Capability
from .tool import Tool, ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_COMMAND_LENGTH = 4_096
MAX_OUTPUT_LENGTH = 100_000
MAX_STDERR_LENGTH = 25_000
DEFAULT_TIMEOUT = 60

# Patterns that are unconditionally blocked.  These target destructive
# filesystem operations, privilege escalation, and code-execution vectors
# where a command is piped from the network directly into an interpreter.
#
# The regex approach has known limits — a clever bypass is possible.
# A container/namespace sandbox is the right long-term answer.
_DANGEROUS_PATTERNS: list[str] = [
    # rm -rf /  or  rm -rf /*  or  rm -rf ~/  or  rm -rf ~/Documents
    r"\brm\b\s+.*(-[^\s]*[rR][^\s]*)\s+(/(\s|$)|/\*|/\.\.\s|~(\s|$|/))",
    # filesystem formatting
    r"\bmkfs\.",
    # raw device writing (dd directly to a block device)
    r"\bdd\b\s+.*\bof\s*=\s*/dev/(sd|hd|nvme|mmcblk|loop|dm-)",
    # shell redirect to block device
    r">\s*/dev/(sd|hd|nvme|mmcblk|loop|dm-)",
    # fork bomb
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
    # system shutdown
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bsystemctl\s+(halt|poweroff|reboot|kexec|suspend|hibernate)\b",
    # privilege escalation
    r"\bsudo\b",
    # curl/wget piped into an interpreter
    r"(curl|wget)\s+.*\|\s*(bash|sh|zsh|dash|python|perl|ruby|lua)",
    # writing to system auth files
    r">\s*/etc/(passwd|shadow|sudoers|ssh/)",
    # changing ownership/permissions on system directories
    r"\bchmod\b\s+.*[47]77\s+/",
    r"\bchown\b\s+(-R\s*)?[^/\s]+:[^/\s]*\s+/",
]

# Substrings that are always blocked regardless of position.
_BLOCKED_SUBSTRINGS: list[str] = [
    "__import__(",
    "eval(",
    "exec(",
    "compile(",
]

# Environment variables to keep in the subprocess.
# Start minimal and add only what's needed.
_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "LD_LIBRARY_PATH",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sandbox_env() -> dict[str, str]:
    """Return a sanitised environment dict containing only allowed keys."""
    env: dict[str, str] = {}
    for key in _ALLOWED_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    # Ensure a safe PATH
    trusted_paths = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/local/sbin",
        "/usr/sbin",
        "/sbin",
    ]
    env.setdefault("PATH", ":".join(trusted_paths))
    return env


def _contains_dangerous_pattern(command: str) -> str | None:
    """Return the first matching dangerous pattern, or None if the command is clean."""
    cmd = command.strip()
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return pattern
    for sub in _BLOCKED_SUBSTRINGS:
        if sub in cmd:
            return sub
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class BashTool(Tool):
    """Execute a shell command in a sandboxed environment.

    The command runs under ``bash -c`` with a timeout, restricted PATH,
    and output length caps.  Destructive commands are blocked before
    execution.
    """

    name = "bash"
    _scopes = {"core", "subagent"}
    _parallel = False
    capabilities = {
        Capability.SHELL, Capability.NETWORK,
        Capability.FILE_READ, Capability.FILE_WRITE,
    }
    description = (
        "Execute a bash shell command and return stdout/stderr. "
        "The command runs in a sandboxed environment with a timeout and "
        "output length limits. DANGEROUS commands (rm -rf /, sudo, curl|sh, "
        "chmod 777 /, etc.) are blocked. "
        "Use this to run build commands, linting, tests, and git operations. "
        "For listing files or exploring directories, use the 'ls' tool instead."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: str | Path,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._timeout = timeout

    # -- execute ---------------------------------------------------------------

    async def execute(self, command: str, **_: Any) -> ToolResult:
        # 1. Validate
        if not command or not command.strip():
            return ToolResult(success=False, content="", error="command must not be empty")

        command = command.strip()
        if len(command) > MAX_COMMAND_LENGTH:
            return ToolResult(
                success=False,
                content="",
                error=f"command too long ({len(command)} > {MAX_COMMAND_LENGTH} chars)",
            )

        blocked = _contains_dangerous_pattern(command)
        if blocked:
            return ToolResult(
                success=False,
                content="",
                error=f"dangerous pattern blocked: {blocked}",
            )

        # 2. Execute
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=_build_sandbox_env(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                content="",
                error=f"command timed out after {self._timeout}s",
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                content="",
                error="bash executable not found on PATH",
            )
        except OSError as exc:
            return ToolResult(
                success=False,
                content="",
                error=f"Cannot execute command: {exc}",
            )

        # 3. Decode & truncate
        parts: list[str] = []
        if stdout_bytes:
            out = stdout_bytes.decode("utf-8", errors="replace")
            if len(out) > MAX_OUTPUT_LENGTH:
                out = out[:MAX_OUTPUT_LENGTH] + "\n... (output truncated)"
            parts.append(out)
        if stderr_bytes:
            err = stderr_bytes.decode("utf-8", errors="replace")
            if len(err) > MAX_STDERR_LENGTH:
                err = err[:MAX_STDERR_LENGTH] + "\n... (stderr truncated)"
            parts.append(f"[stderr]\n{err}")

        content = "\n".join(parts) if parts else "(no output)"
        return ToolResult(
            success=proc.returncode == 0,
            content=content,
            error=None if proc.returncode == 0 else f"exit code: {proc.returncode}",
        )
