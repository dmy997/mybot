"""Sandbox abstraction layer for command execution.

Provides a pluggable backend system for isolating shell commands:

- :class:`NoSandbox` — bare ``subprocess`` (default, no isolation)
- :class:`BubblewrapSandbox` — namespace isolation via ``bwrap``
- :class:`DockerSandbox` — container isolation (future)

Configuration
-------------
``MYBOT_SANDBOX_BACKEND``
    Select the backend (``"none"`` | ``"bubblewrap"`` | ``"docker"``).
    Defaults to ``"none"`` when unset.
"""

from __future__ import annotations

from config import Config

from .base import SandboxBackend, SandboxResult
from .bubblewrap import BubblewrapSandbox
from .none import NoSandbox

__all__ = [
    "SandboxBackend",
    "SandboxResult",
    "NoSandbox",
    "BubblewrapSandbox",
    "create_sandbox",
]


def create_sandbox(
    backend: str | None = None,
    workspace: str = "",
) -> SandboxBackend:
    """Create a sandbox backend instance.

    Parameters
    ----------
    backend:
        Backend name (``"none"`` | ``"bubblewrap"`` | ``"docker"``).
        When ``None``, reads ``MYBOT_SANDBOX_BACKEND`` from the environment,
        defaulting to ``"none"``.
    workspace:
        Absolute path to the agent workspace directory.  Required for
        backends that bind-mount the workspace into the container.
    """
    name = backend or Config.sandbox_backend.strip().lower()

    if name == "bubblewrap":
        return BubblewrapSandbox(workspace)

    if name == "docker":
        raise NotImplementedError("DockerSandbox is not yet implemented")

    return NoSandbox()
