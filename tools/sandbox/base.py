"""Sandbox backend abstract base class and result type.

A ``SandboxBackend`` encapsulates the OS-level execution environment
for shell commands.  Implementations range from a bare ``subprocess``
wrapper (``NoSandbox``) to full namespace/container isolation
(``BubblewrapSandbox``, ``DockerSandbox``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SandboxResult:
    """Normalised result from a sandbox execution."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxBackend(ABC):
    """Abstract sandbox execution environment."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend identifier (e.g. ``"bubblewrap"``)."""
        ...

    @abstractmethod
    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> SandboxResult:
        """Run *command* inside the sandbox and return its result."""
        ...

    @property
    def available(self) -> bool:
        """Check whether this backend is usable on the current host."""
        return True
