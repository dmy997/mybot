"""Plan-and-Solve agent.

Two-phase paradigm:
1. Planning phase — the model generates a step-by-step plan without tools.
2. Execution phase — the model follows the plan and uses tools as needed.
"""

from __future__ import annotations

from core.agent_base import BaseAgent
from core.runner import AgentInput, AgentOutput
from tools.registry import ToolRegistry
from utils import render_template

# Load prompts from templates at import time (no variables, static text)
_PLAN_PROMPT = render_template("agent/plan_solve/plan.md", strip=True)
_EXEC_PROMPT = render_template("agent/plan_solve/execute.md", strip=True)


class PlanSolveAgent(BaseAgent):
    """Agent that plans first, then executes."""

    paradigm = "plan_solve"

    # -- public entry point -------------------------------------------------

    async def run(self, spec: AgentInput) -> AgentOutput:
        # Phase 1: Planning (no tools)
        plan_output = await self._plan(spec)

        if plan_output.stop_reason == "error":
            return plan_output

        # --- HITL plan approval gate ---
        if spec.on_plan_ready is not None and plan_output.content:
            decision = await spec.on_plan_ready("plan_solve", plan_output.content)
            if decision == "denied" or decision == "timeout":
                return AgentOutput(
                    messages=list(spec.init_messages)
                    + [{"role": "assistant", "content": "计划已被用户拒绝，已取消执行。"}],
                    content="计划已被用户拒绝，已取消执行。",
                    usage=plan_output.usage,
                    stop_reason="plan_denied",
                    error="Plan rejected by user"
                    if decision == "denied"
                    else "Plan approval timed out",
                    tool_events=plan_output.tool_events,
                )
            if decision not in ("approved", "denied", "timeout"):
                plan_output = AgentOutput(
                    messages=plan_output.messages,
                    content=decision,
                    usage=plan_output.usage,
                    stop_reason=plan_output.stop_reason,
                    tool_events=plan_output.tool_events,
                )

        # Phase 2: Execution (with tools, plan as context)
        exec_output = await self._execute(spec, plan_output)

        return self._merge_outputs(plan_output, exec_output)

    # -- phases -------------------------------------------------------------

    async def _plan(self, spec: AgentInput) -> AgentOutput:
        plan_spec = self._with_spec(
            spec,
            init_messages=list(spec.init_messages) + [self._user(_PLAN_PROMPT)],
            tools=ToolRegistry(),
        )
        return await self.core.run(plan_spec)

    async def _execute(
        self, spec: AgentInput, plan_output: AgentOutput
    ) -> AgentOutput:
        plan_text = plan_output.content or ""
        base = list(spec.init_messages)
        # Embed plan as a system message so runner compaction never drops it
        base.append({"role": "system", "content": f"[Plan]\n{plan_text}"})
        base.append(self._user(_EXEC_PROMPT))
        exec_spec = self._with_spec(spec, init_messages=base)
        return await self.core.run(exec_spec)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _merge_outputs(plan: AgentOutput, exec_: AgentOutput) -> AgentOutput:
        return AgentOutput(
            messages=exec_.messages,
            tools_used=plan.tools_used + exec_.tools_used,
            content=exec_.content,
            usage=_merge_usage(plan.usage, exec_.usage),
            stop_reason=exec_.stop_reason,
            error=exec_.error,
            tool_events=plan.tool_events + exec_.tool_events,
        )


def _merge_usage(
    a: dict[str, int], b: dict[str, int]
) -> dict[str, int]:
    """Sum token usage from two phases."""
    merged: dict[str, int] = dict(a)
    for k, v in b.items():
        merged[k] = merged.get(k, 0) + v
    return merged
