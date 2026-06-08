"""Sub-agent delegation tool.

Allows the main agent to spawn isolated sub-agents for focused subtasks.
Each sub-agent runs its own :class:`AgentCore` loop with a restricted tool
set.  Sub-agents are NOT auto-discovered — they are registered manually by
the Orchestrator.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from observability.metrics import REGISTRY
from observability.trace import tracer
from utils import render_template

from .guard import Capability
from .registry import ToolRegistry
from .tool import Tool, ToolResult

# ---------------------------------------------------------------------------
# SubAgentSpec
# ---------------------------------------------------------------------------


_SUBAGENT_SYSTEM_PROMPT = render_template("agent/subagent_system.md", strip=True)


@dataclass
class SubAgentSpec:
    """Complete configuration for a single sub-agent run."""

    task: str
    tools: ToolRegistry
    system_prompt: str = _SUBAGENT_SYSTEM_PROMPT
    max_iterations: int = 10
    model: str | None = None
    timeout_seconds: float = 120.0
    allow_network: bool = False   # sub-agents cannot access network by default
    allow_shell: bool = False     # sub-agents cannot execute shell by default


# ---------------------------------------------------------------------------
# SubAgentTool
# ---------------------------------------------------------------------------


class SubAgentTool(Tool):
    """Tool that spawns an isolated sub-agent to complete a delegated task.

    The sub-agent runs its own :class:`AgentCore` loop with a restricted
    tool subset.  Results are returned inline so the parent agent can continue
    reasoning with the sub-agent's output in context.

    Sub-agents do NOT have access to this tool, preventing unbounded recursion.
    """

    name = "delegate"
    _scopes = {"core"}  # only available to the main agent
    _parallel = True    # independent sub-agents can run concurrently
    capabilities = {Capability.DELEGATE}
    description = (
        "将子任务委托给一个独立的子 Agent 执行。子 Agent 拥有独立的执行上下文"
        "和受限的工具集，完成后返回结果。\n\n"
        "适用场景：需要跨多个文件搜索、分析项目结构、执行独立的调查或分析子任务。\n"
        "不适用：简单的单步操作（如「读取一个已知文件」）。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "子 Agent 需要完成的具体任务描述。越具体越好，包括期望的"
                    "输出格式和搜索/操作范围。"
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

        # Sub-agents get all parent tools minus delegate (no recursion)
        spec = SubAgentSpec(
            task=task,
            tools=ToolRegistry(),  # placeholder — replaced below
        )
        spec.tools = self._build_sub_tools(spec)

        logger.info(
            "Sub-agent starting: task={task!r}, tools={tools}",
            task=task[:200],
            tools=[t.name for t in spec.tools],
        )
        REGISTRY.tool_calls_total.inc()

        t_start = time.monotonic()

        with tracer.span("subagent.run", task=task[:200]):
            result = await self._run_sub_agent(spec)

        latency_ms = (time.monotonic() - t_start) * 1000
        REGISTRY.tool_latency_ms.observe(latency_ms)

        if not result.success:
            REGISTRY.tool_calls_errors_total.inc()
            logger.warning("Sub-agent failed: {error}", error=result.error)

        return result

    # -- internal --------------------------------------------------------------

    def _build_sub_tools(self, spec: SubAgentSpec) -> ToolRegistry:
        """Build a tool registry with sub-agent ToolGuard, excluding delegate."""
        from .guard import ToolGuard as _ToolGuard

        guard = _ToolGuard(
            self._workspace,
            scope="subagent",
            allow_network=spec.allow_network,
            allow_shell=spec.allow_shell,
        )
        sub = ToolRegistry(guard=guard)
        for tool in self._parent_registry:
            if tool.name != "delegate":
                sub.register(tool)
        return sub

    async def _run_sub_agent(self, spec: SubAgentSpec) -> ToolResult:
        """Execute the sub-agent's AgentCore loop with timeout protection."""
        from core.runner import AgentCore, AgentInput

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": spec.system_prompt},
            {"role": "user", "content": spec.task},
        ]

        core = AgentCore(
            self._provider,
            max_iterations=spec.max_iterations,
        )

        agent_input = AgentInput(
            init_messages=messages,
            tools=spec.tools,
            model=spec.model,
        )

        try:
            output = await asyncio.wait_for(
                core.run(agent_input),
                timeout=spec.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                content="",
                error=f"子代理超时（{spec.timeout_seconds:.0f}s）",
            )

        if output.error:
            return ToolResult(
                success=False,
                content=output.content or "",
                error=f"子代理错误: {output.error}",
            )

        # Format a clean result for the parent agent
        result_parts = [
            "子任务完成",
            "=" * 10,
            "",
            output.content,
        ]

        if output.tools_used:
            result_parts.append("")
            result_parts.append(f"(使用工具: {', '.join(output.tools_used)})")

        return ToolResult(success=True, content="\n".join(result_parts))
