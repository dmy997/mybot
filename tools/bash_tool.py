"""BashTool — execute shell commands with sandbox restrictions.

Commands are validated against a blocklist of dangerous patterns, run with a
timeout, and their output is capped to prevent context pollution.

Execution is delegated to a :class:`~tools.sandbox.base.SandboxBackend`
so the OS-level isolation strategy (none, bubblewrap, docker) is pluggable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .guard import Capability
from .sandbox import SandboxBackend, create_sandbox
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
        "Use for: build commands (npm/pip/cargo), linting/testing, git operations, "
        "package management. "
        "NOT for: listing files (use ls), reading files (use read), "
        "searching text (use grep), writing files (use write). "
        "The command runs in a sandboxed environment with a timeout and "
        "output length limits. DANGEROUS commands (rm -rf /, sudo, curl|sh, "
        "chmod 777 /, etc.) are blocked."
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
        sandbox: SandboxBackend | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._timeout = timeout
        self._sandbox = sandbox or create_sandbox(workspace=str(self.workspace))

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

        # 2. Execute via sandbox backend
        result = await self._sandbox.execute(
            command,
            cwd=str(self.workspace),
            env=_build_sandbox_env(),
            timeout=self._timeout,
        )

        if result.timed_out:
            return ToolResult(
                success=False,
                content="",
                error=f"command timed out after {self._timeout}s",
            )

        if result.exit_code == -1 and not result.success:
            return ToolResult(
                success=False,
                content="",
                error=result.stderr.strip() or "sandbox execution failed",
            )

        # 3. Truncate output
        parts: list[str] = []
        if result.stdout:
            out = result.stdout
            if len(out) > MAX_OUTPUT_LENGTH:
                out = out[:MAX_OUTPUT_LENGTH] + "\n... (output truncated)"
            parts.append(out)
        if result.stderr:
            err = result.stderr
            if len(err) > MAX_STDERR_LENGTH:
                err = err[:MAX_STDERR_LENGTH] + "\n... (stderr truncated)"
            parts.append(f"[stderr]\n{err}")

        content = "\n".join(parts) if parts else "(no output)"
        return ToolResult(
            success=result.success,
            content=content,
            error=None if result.success else f"exit code: {result.exit_code}",
        )
