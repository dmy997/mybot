"""Orchestrator-workers topology — the multi-agent coordination mechanics.

Three phases, application-agnostic:

1. **Decompose** — the lead runs a tool-less planning loop and emits a JSON
   array of independent subtasks.
2. **Fan out** — one worker sub-agent per subtask runs in parallel (bounded
   concurrency) via :class:`~agents.team.runner.SubAgentRunner`.
3. **Synthesize** — a tool-less loop fuses the worker findings into a full
   report plus a short executive summary.

The topology knows nothing about any specific application (DeepResearch,
committee review, …) — that lives in a :class:`~agents.team.blueprint.TeamBlueprint`.
Partial worker failure degrades gracefully: synthesis proceeds with whatever
succeeded, and failures are recorded on the returned :class:`TeamResult`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from tools import ToolRegistry

from .blueprint import TeamBlueprint
from .runner import SubAgentResult, SubAgentRunner, SubAgentSpec

_TAG_RE = "<{tag}>(.*?)</{tag}>"


@dataclass
class TeamResult:
    """Aggregate result of one orchestrator-workers run."""

    full_report: str
    summary: str
    subtasks: list[str] = field(default_factory=list)
    worker_results: list[SubAgentResult] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""


class OrchestratorWorkers:
    """Drive a :class:`TeamBlueprint` through decompose → fan-out → synthesize.

    Parameters
    ----------
    core:
        The lead :class:`~core.runner.AgentCore` used for the tool-less
        decompose and synthesize loops.  ``core.provider`` also seeds the
        worker runner.
    runner:
        The :class:`SubAgentRunner` used for the parallel worker fan-out.
    """

    def __init__(self, core: Any, runner: SubAgentRunner) -> None:
        self.core = core
        self.runner = runner

    # -- public ---------------------------------------------------------------

    async def execute(
        self,
        topic: str,
        blueprint: TeamBlueprint,
        parent_tools: ToolRegistry,
    ) -> TeamResult:
        """Run the full topology for *topic* under *blueprint*.

        Supports multi-round refinement: after the first fan-out + synthesis,
        if the report identifies coverage gaps, the lead spawns a second
        (smaller) wave of workers to fill them, up to *max_rounds*.
        """
        # -- Round 1: decompose → fan-out → synthesize --------------------------
        subtasks = await self._decompose(topic, blueprint, parent_tools)
        if not subtasks:
            return TeamResult(
                full_report="",
                summary="",
                error="lead 未能将主题分解为子任务",
            )

        worker_tools = self._select_tools(parent_tools, blueprint.worker.tool_names)
        all_results: list[SubAgentResult] = []
        all_subtasks: list[str] = list(subtasks)

        results = await self._run_fan_out(subtasks, blueprint, worker_tools)
        all_results.extend(results)

        full_report, summary = await self._synthesize(
            topic, subtasks, results, blueprint
        )

        # -- Refinement round(s): gap detection → extra workers → re-synthesize --
        for rnd in range(2, blueprint.max_rounds + 1):
            gap_subtasks = await self._detect_gaps(
                topic, full_report, blueprint, parent_tools
            )
            if not gap_subtasks:
                break

            cap = max(1, blueprint.max_workers // 2)
            gap_subtasks = gap_subtasks[:cap]
            logger.info(
                "Team '{}' round {}: {} gap-filling subtasks",
                blueprint.name, rnd, len(gap_subtasks),
            )

            extra = await self._run_fan_out(gap_subtasks, blueprint, worker_tools)
            all_results.extend(extra)
            all_subtasks.extend(gap_subtasks)

            full_report, summary = await self._synthesize(
                topic, all_subtasks, all_results, blueprint
            )

        return TeamResult(
            full_report=full_report,
            summary=summary,
            subtasks=all_subtasks,
            worker_results=all_results,
            usage=self._sum_usage(all_results),
        )

    # -- phases ---------------------------------------------------------------

    async def _decompose(self, topic: str, blueprint: TeamBlueprint, parent_tools: ToolRegistry) -> list[str]:
        from core.runner import AgentInput

        lead_tools = self._select_tools(parent_tools, ("websearch", "webfetch"))

        messages = [
            {"role": "system", "content": blueprint.lead_prompt},
            {
                "role": "user",
                "content": (
                    f"研究主题：{topic}\n\n"
                    f"请将其分解为最多 {blueprint.max_workers} 个相互独立的子任务，"
                    "只输出一个 JSON 字符串数组，不要额外说明。"
                ),
            },
        ]
        out = await self.core.run(
            AgentInput(
                init_messages=messages,
                tools=lead_tools,
                model=blueprint.lead_model,
            )
        )
        return self._parse_subtasks(out.content, blueprint.max_workers)

    async def _run_fan_out(
        self,
        subtasks: list[str],
        blueprint: TeamBlueprint,
        worker_tools: ToolRegistry,
    ) -> list[SubAgentResult]:
        """Spawn and run workers for *subtasks*, return their results."""
        specs = [
            SubAgentSpec(
                task=st,
                system_prompt=blueprint.worker.system_prompt,
                tools=worker_tools,
                model=blueprint.worker.model,
                max_iterations=blueprint.worker.max_iterations,
                timeout_seconds=blueprint.worker.timeout_seconds,
                allow_network=blueprint.worker.allow_network,
                allow_shell=blueprint.worker.allow_shell,
            )
            for st in subtasks
        ]
        results = await self.runner.run_all(
            specs, max_concurrent=blueprint.max_concurrent
        )
        ok = sum(1 for r in results if r.success)
        logger.info(
            "Team '{}' fan-out: {}/{} workers succeeded",
            blueprint.name, ok, len(results),
        )
        return results

    async def _detect_gaps(
        self,
        topic: str,
        report: str,
        blueprint: TeamBlueprint,
        parent_tools: ToolRegistry,
    ) -> list[str]:
        """Ask the lead to identify remaining coverage gaps from the report.

        Returns a (possibly empty) list of gap-filling subtask strings.
        """
        from core.runner import AgentInput

        lead_tools = self._select_tools(parent_tools, ("websearch", "webfetch"))
        messages = [
            {"role": "system", "content": blueprint.lead_prompt},
            {
                "role": "user",
                "content": (
                    f"研究主题：{topic}\n\n"
                    f"以下是一份初步研究报告：\n\n{report[:3000]}\n\n"
                    "请对照原始研究主题，检查报告中是否存在明显的信息缺口"
                    "（如遗漏了某个实体、某个维度、或缺少关键数据）。"
                    "如有缺口，请将其分解为最多 3 个补充研究子任务，"
                    "输出一个 JSON 字符串数组。"
                    "如果没有明显缺口，输出一个空数组 []。"
                ),
            },
        ]
        out = await self.core.run(
            AgentInput(
                init_messages=messages,
                tools=lead_tools,
                model=blueprint.lead_model,
            )
        )
        return self._parse_subtasks(out.content, 4)

    async def _synthesize(
        self,
        topic: str,
        subtasks: list[str],
        results: list[SubAgentResult],
        blueprint: TeamBlueprint,
    ) -> tuple[str, str]:
        from core.runner import AgentInput

        findings = self._format_findings(subtasks, results)
        messages = [
            {"role": "system", "content": blueprint.synthesis_prompt},
            {
                "role": "user",
                "content": f"研究主题：{topic}\n\n各子任务的调研发现：\n\n{findings}",
            },
        ]
        out = await self.core.run(
            AgentInput(
                init_messages=messages,
                tools=ToolRegistry(),
                model=blueprint.synthesis_model,
            )
        )
        return self._split_report(out.content or "")

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _select_tools(parent: ToolRegistry, names: tuple[str, ...]) -> ToolRegistry:
        """Select a worker tool subset from the parent registry.

        Always excludes ``delegate`` (workers cannot spawn more workers).
        Empty *names* means "all parent tools minus delegate".
        """
        reg = ToolRegistry()
        for tool in parent:
            if tool.name == "delegate":
                continue
            if names and tool.name not in names:
                continue
            reg.register(tool)
        return reg

    @staticmethod
    def _parse_subtasks(content: str | None, cap: int) -> list[str]:
        """Extract a list of subtask strings from the lead's output.

        Tolerant: tries a JSON array between the first ``[`` and last ``]``;
        falls back to line-based extraction of ``- ``/``1.`` bullets.
        """
        text = (content or "").strip()
        start, end = text.find("["), text.rfind("]")
        if 0 <= start < end:
            try:
                data = json.loads(text[start : end + 1])
                items = [str(x).strip() for x in data if str(x).strip()]
                if items:
                    return items[:cap]
            except (ValueError, TypeError):
                pass
        # Fallback: bullet / numbered lines
        items = []
        for line in text.splitlines():
            stripped = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            if stripped and not stripped.startswith(("```", "[", "]")):
                items.append(stripped)
        return items[:cap]

    @staticmethod
    def _format_findings(
        subtasks: list[str], results: list[SubAgentResult]
    ) -> str:
        parts: list[str] = []
        for i, (task, res) in enumerate(zip(subtasks, results), start=1):
            parts.append(f"### 子任务 {i}: {task}")
            if res.success:
                parts.append(res.content or "（无内容）")
            else:
                parts.append(f"⚠️ 该子任务失败：{res.error}")
            parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _split_report(content: str) -> tuple[str, str]:
        """Split synthesis output into (full_report, summary) by tags.

        Expects ``<summary>…</summary>`` and ``<report>…</report>``; falls
        back to using the whole text as the report and its head as summary.
        """
        summary = _extract_tag(content, "summary")
        report = _extract_tag(content, "report")
        if not report:
            report = content.strip()
        if not summary:
            summary = report[:400]
        return report, summary

    @staticmethod
    def _sum_usage(results: list[SubAgentResult]) -> dict[str, int]:
        total: dict[str, int] = {}
        for r in results:
            for k, v in r.usage.items():
                total[k] = total.get(k, 0) + v
        return total


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(_TAG_RE.format(tag=tag), text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""
