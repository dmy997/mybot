"""Sub-agent delegation tool.

Allows the main agent to spawn an isolated sub-agent for a focused subtask.
The worker execution is delegated to the shared
:class:`~agents.team.runner.SubAgentRunner` — the same primitive the
multi-agent DeepResearch topology uses — so there is a single sub-agent
runtime across the codebase.

Each sub-agent runs its own :class:`AgentCore` loop with a restricted tool
set (parent tools minus ``delegate``, guarded by a ``subagent``-scope
ToolGuard).  Sub-agents do NOT have access to this tool, preventing
unbounded recursion.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from observability.metrics import REGISTRY
from observability.trace import tracer
from utils import render_template

from .guard import Capability
from .registry import ToolRegistry
from .tool import Tool, ToolResult

_SUBAGENT_SYSTEM_PROMPT = render_template("agent/subagent_system.md", strip=True)
_MAX_ITERATIONS = 10
_TIMEOUT_SECONDS = 120.0


class SubAgentTool(Tool):
    """Tool that spawns an isolated sub-agent to complete a delegated task.

    Results are returned inline so the parent agent can continue reasoning
    with the sub-agent's output in context.
    """

    name = "delegate"
    _scopes = {"core"}  # only available to the main agent
    _parallel = True    # independent sub-agents can run concurrently
    capabilities = {Capability.DELEGATE}
    description = (
        "Delegate a sub-task to an independent sub-agent for execution. "
        "The sub-agent has its own execution context and a restricted tool set. "
        "Use for: cross-file searches, project structure analysis, or independent "
        "research sub-tasks. NOT for simple single-step operations."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Detailed description of the sub-task to complete. Include "
                    "expected output format and search/operation scope."
                ),
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        provider: Any,
        parent_registry: ToolRegistry,
        *,
        workspace: str | Path | None = None,
    ) -> None:
        """
        Parameters
        ----------
        provider:
            LLM provider shared with the parent agent.
        parent_registry:
            The parent's full tool registry.  Sub-agents inherit all tools
            except ``delegate`` (no recursion).
        workspace:
            Root directory for the sub-agent's ToolGuard file-path validation.
            Defaults to the current working directory when not set.
        """
        self._provider = provider
        self._parent_registry = parent_registry
        _ws = Path(workspace) if workspace else Path.cwd()
        self._workspace = _ws.resolve()

    # -- execute ---------------------------------------------------------------

    async def execute(self, task: str, **_: Any) -> ToolResult:
        """Spawn and run a sub-agent for the given *task*."""
        from agents.team.runner import SubAgentRunner, SubAgentSpec

        runner = SubAgentRunner(self._provider, workspace=self._workspace)
        sub_tools = self._sub_tools()
        spec = SubAgentSpec(
            task=task,
            system_prompt=_SUBAGENT_SYSTEM_PROMPT,
            tools=sub_tools,
            max_iterations=_MAX_ITERATIONS,
            timeout_seconds=_TIMEOUT_SECONDS,
        )

        logger.info(
            "Sub-agent starting: task={task!r}, tools={tools}",
            task=task[:200],
            tools=[t.name for t in sub_tools],
        )
        REGISTRY.tool_calls_total.inc()

        t_start = time.monotonic()
        with tracer.span("subagent.run", task=task[:200]):
            result = await runner.run(spec)
        REGISTRY.tool_latency_ms.observe((time.monotonic() - t_start) * 1000)

        if not result.success:
            REGISTRY.tool_calls_errors_total.inc()
            logger.warning("Sub-agent failed: {error}", error=result.error)
            return ToolResult(
                success=False, content=result.content, error=result.error
            )

        result_parts = [
            "子任务完成",
            "=" * 10,
            "",
            result.content,
        ]
        if result.tools_used:
            result_parts.append("")
            result_parts.append(f"(使用工具: {', '.join(result.tools_used)})")

        return ToolResult(success=True, content="\n".join(result_parts))

    # -- internal --------------------------------------------------------------

    def _sub_tools(self) -> ToolRegistry:
        """Parent tools minus ``delegate`` (no recursion).

        The runner re-wraps these with a ``subagent``-scope ToolGuard, so no
        guard is attached here.
        """
        reg = ToolRegistry()
        for tool in self._parent_registry:
            if tool.name != "delegate":
                reg.register(tool)
        return reg
