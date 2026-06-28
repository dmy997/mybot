"""BubblewrapSandbox — OS-level namespace isolation via ``bwrap``.

Uses `Bubblewrap <https://github.com/containers/bubblewrap>`_ to create
an unprivileged container for each command execution.  This provides
defence-in-depth beyond regex-based pattern blocking:

- PID, IPC, UTS, cgroup namespaces are isolated
- All capabilities are dropped
- Filesystem is read-only except for the workspace directory
- ``/tmp`` is a private tmpfs
- Environment is sanitised via ``--clearenv`` + ``--setenv``
- Process dies with the parent (``--die-with-parent``)

Network access is preserved (``--share-net``) because the BashTool
declares ``NETWORK`` capability.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import SandboxBackend, SandboxResult

# Paths that are always bind-mounted read-only.
_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/etc",
)


class BubblewrapSandbox(SandboxBackend):
    """Run each command in a fresh Bubblewrap container."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).resolve()
        self._bwrap_path = shutil.which("bwrap") or "bwrap"

    @property
    def name(self) -> str:
        return "bubblewrap"

    @property
    def available(self) -> bool:
        return shutil.which("bwrap") is not None

    # -- execute ---------------------------------------------------------------

    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> SandboxResult:
        bwrap_args = self._build_bwrap_args(command, cwd=cwd, env=env)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._bwrap_path,
                *bwrap_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return SandboxResult(
                exit_code=-1,
                timed_out=True,
                stderr=f"command timed out after {timeout}s",
            )
        except FileNotFoundError:
            return SandboxResult(
                exit_code=-1,
                stderr="bwrap executable not found — install bubblewrap to use this backend",
            )
        except OSError as exc:
            return SandboxResult(
                exit_code=-1,
                stderr=f"Cannot execute command: {exc}",
            )

        return SandboxResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            exit_code=proc.returncode or 0,
        )

    # -- bwrap args ------------------------------------------------------------

    def _build_bwrap_args(
        self, command: str, *, cwd: str, env: dict[str, str]
    ) -> list[str]:
        """Construct the ``bwrap`` argument list."""
        args: list[str] = [
            # Isolate everything except network
            "--unshare-all",
            "--share-net",
            # Drop all capabilities inside the container
            "--cap-drop", "ALL",
            # Standard kernel filesystems
            "--proc", "/proc",
            "--dev", "/dev",
            # Private /tmp
            "--tmpfs", "/tmp",
        ]

        # Read-only system binds (skip paths that don't exist on this host)
        for path in _RO_BINDS:
            if Path(path).exists():
                args += ["--ro-bind", path, path]

        # Read-write workspace bind
        workspace = str(self._workspace)
        args += ["--bind", workspace, workspace]

        # Clean environment, then set only allowed vars
        args.append("--clearenv")
        for key, value in sorted(env.items()):
            args += ["--setenv", key, value]

        # Die when the parent exits (no orphaned bwrap processes)
        args += ["--die-with-parent"]

        # Set working directory inside the container
        args += ["--chdir", cwd]

        # The actual command
        args += ["bash", "-c", command]

        return args
