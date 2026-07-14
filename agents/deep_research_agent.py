"""DeepResearch paradigm — orchestrator-workers over the deep_research blueprint.

A thin :class:`BaseAgent` that binds the generic
:class:`~agents.team.topology.OrchestratorWorkers` mechanics to the
``deep_research`` blueprint.  Routed via the ``/research`` command
(see :mod:`core.dispatcher`).

The lead decomposes the topic, workers research in parallel (web search +
fetch), and a synthesizer fuses the findings into a full report + short
summary.  The full report is archived under ``{workspace}/research/`` and the
summary (plus the file path) is returned as the streamed reply — ideal for a
weekly scheduled push where a wall-of-text report would be unreadable.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

from agents.team.blueprint import TeamBlueprint, WorkerRole
from agents.team.runner import SubAgentRunner
from agents.team.topology import OrchestratorWorkers
from config import Config
from core.agent_base import BaseAgent
from core.runner import AgentInput, AgentOutput
from utils import render_template

_LEAD_PROMPT = render_template("agent/deep_research/lead.md", strip=True)
_WORKER_PROMPT = render_template("agent/deep_research/worker.md", strip=True)
_SYNTH_PROMPT = render_template("agent/deep_research/synthesize.md", strip=True)

DEEP_RESEARCH = TeamBlueprint(
    name="deep_research",
    lead_prompt=_LEAD_PROMPT,
    worker=WorkerRole(
        system_prompt=_WORKER_PROMPT,
        tool_names=("websearch", "webfetch"),
        allow_network=True,
        max_iterations=12,
        timeout_seconds=300.0,
    ),
    synthesis_prompt=_SYNTH_PROMPT,
    max_workers=8,
    max_concurrent=4,
)

_CMD_RE = re.compile(r"^/research\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^\w一-鿿]+")


class DeepResearchAgent(BaseAgent):
    """Multi-agent DeepResearch paradigm (``/research``)."""

    paradigm = "deep_research"

    async def run(self, spec: AgentInput) -> AgentOutput:
        topic = self._extract_topic(spec)
        if not topic:
            return self._fail(spec, "未能识别研究主题")

        logger.info("DeepResearch starting: topic={!r}", topic[:120])

        async def _progress(msg: str) -> None:
            if spec.on_content_delta:
                await spec.on_content_delta(msg + "\n\n")

        await _progress(f"🔬 **DeepResearch 启动**：{topic[:100]}")

        runner = SubAgentRunner(self.core.provider, workspace=self._workspace_root())
        topo = OrchestratorWorkers(self.core, runner)
        team = await topo.execute(
            topic, DEEP_RESEARCH, spec.tools, on_progress=_progress,
        )

        if team.error and not team.full_report:
            return self._fail(spec, team.error)

        report_path = self._save_report(topic, team.full_report)
        content = self._format_output(topic, team.summary, report_path, team)

        if spec.on_content_delta:
            await spec.on_content_delta(content)

        messages = list(spec.init_messages) + [
            {"role": "assistant", "content": content}
        ]
        return AgentOutput(
            messages=messages,
            content=content,
            tools_used=["deep_research"],
            usage=team.usage,
            stop_reason="completed",
            tool_events=self._worker_events(team),
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _extract_topic(spec: AgentInput) -> str:
        if spec.goal:
            return spec.goal.strip()
        for msg in reversed(spec.init_messages):
            if msg.get("role") == "user":
                text = str(msg.get("content", "")).strip()
                stripped = _CMD_RE.sub("", text).strip()
                return stripped or text
        return ""

    def _workspace_root(self) -> Path:
        return Path(Config.workspace).expanduser().resolve()

    def _save_report(self, topic: str, report: str) -> Path:
        research_dir = self._workspace_root() / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        slug = _SLUG_RE.sub("-", topic).strip("-")[:40] or "report"
        path = research_dir / f"{date.today().isoformat()}_{slug}.md"
        path.write_text(report, encoding="utf-8")
        logger.info("DeepResearch report saved: {}", path)
        return path

    @staticmethod
    def _format_output(
        topic: str, summary: str, path: Path, team: Any
    ) -> str:
        ok = sum(1 for r in team.worker_results if r.success)
        total = len(team.worker_results)
        lines = [
            f"🔬 **DeepResearch 完成：{topic}**",
            "",
            summary.strip() or "（无摘要）",
            "",
            f"📄 完整报告已保存：`{path}`",
            f"👥 {ok}/{total} 个子任务成功",
        ]
        return "\n".join(lines)

    @staticmethod
    def _worker_events(team: Any) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        for i, res in enumerate(team.worker_results, start=1):
            events.append(
                {
                    "tool": f"worker[{i}]",
                    "status": "ok" if res.success else "error",
                    "summary": (res.task or "")[:80],
                }
            )
        return events

    @staticmethod
    def _fail(spec: AgentInput, reason: str) -> AgentOutput:
        content = f"DeepResearch 失败：{reason}"
        return AgentOutput(
            messages=list(spec.init_messages)
            + [{"role": "assistant", "content": content}],
            content=content,
            stop_reason="error",
            error=reason,
        )
