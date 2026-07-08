"""Sub-agent runtime — isolated AgentCore loop with tool restrictions.

A standalone worker runner that spawns one :class:`AgentCore` per sub-agent,
with independent message context, restricted tools, and wall-clock timeout.
Supports parallel fan-out via :meth:`SubAgentRunner.run_all`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from tools import ToolRegistry

# ---------------------------------------------------------------------------
# Spec & result
# ---------------------------------------------------------------------------


@dataclass
class SubAgentSpec:
    """Complete configuration for a single sub-agent run."""

    task: str
    """The instruction / prompt for the sub-agent (user message)."""
    system_prompt: str
    """System-level instructions for the sub-agent."""
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    """Tool subset available to this sub-agent."""
    model: str | None = None
    """Optional model override."""
    max_iterations: int = 10
    timeout_seconds: float = 120.0
    # Guard controls
    allow_network: bool = False
    allow_shell: bool = False


@dataclass
class SubAgentResult:
    """Structured result from a single sub-agent run."""

    success: bool
    content: str
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    task: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class SubAgentRunner:
    """Spawn and run an isolated sub-agent with its own :class:`AgentCore`.

    Parameters
    ----------
    provider:
        LLM provider shared with the parent agent (taken from
        ``AgentCore.provider``).
    workspace:
        Root directory for the sub-agent's ToolGuard file-path validation.
    """

    def __init__(self, provider: Any, *, workspace: str | Path | None = None) -> None:
        self._provider = provider
        self._workspace = (Path(workspace) if workspace else Path.cwd()).resolve()

    # -- single ---------------------------------------------------------------

    async def run(self, spec: SubAgentSpec) -> SubAgentResult:
        """Execute one sub-agent with timeout protection.

        Returns a :class:`SubAgentResult` — never raises; a timeout or crash
        is captured as ``success=False`` with the reason in ``error``.
        """
        from core.runner import AgentCore, AgentInput

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": spec.system_prompt},
            {"role": "user", "content": spec.task},
        ]

        sub_tools = self._build_tools(spec)
        core = AgentCore(self._provider, max_iterations=spec.max_iterations)
        agent_input = AgentInput(
            init_messages=messages,
            tools=sub_tools,
            model=spec.model,
        )

        t_start = time.monotonic()
        try:
            output = await asyncio.wait_for(
                core.run(agent_input),
                timeout=spec.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return SubAgentResult(
                success=False,
                content="",
                task=spec.task,
                error=f"子代理超时（{spec.timeout_seconds:.0f}s）",
            )
        except Exception as exc:
            return SubAgentResult(
                success=False,
                content="",
                task=spec.task,
                error=f"子代理异常: {exc}",
            )

        latency_ms = (time.monotonic() - t_start) * 1000
        logger.debug(
            "Sub-agent finished: {:.0f}ms, tools={}", latency_ms, output.tools_used
        )

        if output.error:
            return SubAgentResult(
                success=False,
                content=output.content or "",
                tools_used=output.tools_used,
                usage=output.usage,
                task=spec.task,
                error=output.error,
            )

        return SubAgentResult(
            success=True,
            content=output.content,
            tools_used=output.tools_used,
            usage=output.usage,
            task=spec.task,
        )

    # -- parallel fan-out -----------------------------------------------------

    async def run_all(
        self,
        specs: list[SubAgentSpec],
        *,
        max_concurrent: int = 4,
    ) -> list[SubAgentResult]:
        """Run multiple sub-agents in parallel with a concurrency cap.

        Uses ``asyncio.Semaphore`` to bound concurrent LLM calls.  Individual
        failures do not cancel siblings (each :meth:`run` captures its own
        errors); results are returned in input order.
        """
        if not specs:
            return []
        sem = asyncio.Semaphore(max(1, max_concurrent))

        async def _bounded(spec: SubAgentSpec) -> SubAgentResult:
            async with sem:
                return await self.run(spec)

        return await asyncio.gather(*(_bounded(s) for s in specs))

    # -- internal -------------------------------------------------------------

    def _build_tools(self, spec: SubAgentSpec) -> ToolRegistry:
        """Build an isolated tool registry with a scoped guard."""
        from tools.guard import ToolGuard

        guard = ToolGuard(
            self._workspace,
            scope="subagent",
            allow_network=spec.allow_network,
            allow_shell=spec.allow_shell,
        )
        sub = ToolRegistry(guard=guard)
        for tool in spec.tools:
            sub.register(tool)
        return sub
