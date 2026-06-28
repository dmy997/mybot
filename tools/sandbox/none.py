"""NoSandbox — bare ``subprocess`` backend with no OS-level isolation.

This is the default backend.  It runs commands directly via
``asyncio.create_subprocess_exec``, identical to the original
BashTool behaviour before the sandbox abstraction was introduced.
"""

from __future__ import annotations

import asyncio

from .base import SandboxBackend, SandboxResult


class NoSandbox(SandboxBackend):
    """Run commands directly on the host with no container/namespace isolation."""

    @property
    def name(self) -> str:
        return "none"

    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> SandboxResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
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
                stderr="bash executable not found on PATH",
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
