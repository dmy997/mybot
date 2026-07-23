"""Tests for the multi-agent DeepResearch paradigm.

Covers the sub-agent runner (success / timeout / crash / parallel), the
TeamBlueprint validation, the OrchestratorWorkers topology (decompose parse,
report split, tool selection, execute happy / empty / partial-failure), the
thin DeepResearchAgent (report archival, topic extraction, failure), and the
``/research`` dispatcher route.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agents.deep_research_agent import DeepResearchAgent
from agents.team.blueprint import TeamBlueprint, WorkerRole
from agents.team.runner import SubAgentResult, SubAgentRunner, SubAgentSpec
from agents.team.topology import OrchestratorWorkers, TeamResult
from core.dispatcher import Dispatcher, _match_explicit_command
from core.runner import AgentInput, AgentOutput
from tools import ToolRegistry

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCore:
    """Stands in for AgentCore: scripted ``run()`` returning queued outputs."""

    def __init__(self, outputs: list[AgentOutput]) -> None:
        self._outputs = list(outputs)
        self.provider = object()
        self.calls: list[AgentInput] = []

    async def run(self, spec: AgentInput) -> AgentOutput:
        self.calls.append(spec)
        return self._outputs.pop(0)


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _blueprint(**kw) -> TeamBlueprint:
    return TeamBlueprint(
        name=kw.get("name", "deep_research"),
        lead_prompt="lead",
        worker=WorkerRole(
            system_prompt="worker",
            tool_names=kw.get("tool_names", ("websearch",)),
            allow_network=True,
        ),
        synthesis_prompt="synth",
        max_workers=kw.get("max_workers", 5),
        max_concurrent=kw.get("max_concurrent", 3),
    )


# ---------------------------------------------------------------------------
# SubAgentRunner
# ---------------------------------------------------------------------------


class TestSubAgentRunner:
    async def test_run_success(self):
        with patch("core.runner.AgentCore") as mock_core:
            mock_core.return_value.run = AsyncMock(
                return_value=AgentOutput(content="done", tools_used=["websearch"])
            )
            runner = SubAgentRunner(provider=object())
            res = await runner.run(SubAgentSpec(task="t", system_prompt="s"))
        assert res.success
        assert res.content == "done"
        assert res.task == "t"
        assert res.tools_used == ["websearch"]

    async def test_run_timeout(self):
        async def _slow(_spec):
            await asyncio.sleep(1)

        with patch("core.runner.AgentCore") as mock_core:
            mock_core.return_value.run = _slow
            runner = SubAgentRunner(provider=object())
            res = await runner.run(
                SubAgentSpec(task="t", system_prompt="s", timeout_seconds=0.01)
            )
        assert not res.success
        assert "超时" in res.error

    async def test_run_crash_captured(self):
        with patch("core.runner.AgentCore") as mock_core:
            mock_core.return_value.run = AsyncMock(side_effect=RuntimeError("boom"))
            runner = SubAgentRunner(provider=object())
            res = await runner.run(SubAgentSpec(task="t", system_prompt="s"))
        assert not res.success
        assert "异常" in res.error

    async def test_run_propagates_output_error(self):
        with patch("core.runner.AgentCore") as mock_core:
            mock_core.return_value.run = AsyncMock(
                return_value=AgentOutput(content="", error="llm failed")
            )
            runner = SubAgentRunner(provider=object())
            res = await runner.run(SubAgentSpec(task="t", system_prompt="s"))
        assert not res.success
        assert res.error == "llm failed"

    async def test_run_all_parallel_preserves_order(self):
        with patch("core.runner.AgentCore") as mock_core:
            mock_core.return_value.run = AsyncMock(return_value=AgentOutput(content="x"))
            runner = SubAgentRunner(provider=object())
            specs = [
                SubAgentSpec(task=f"t{i}", system_prompt="s") for i in range(3)
            ]
            res = await runner.run_all(specs, max_concurrent=2)
        assert [r.task for r in res] == ["t0", "t1", "t2"]
        assert all(r.success for r in res)

    async def test_run_all_empty(self):
        runner = SubAgentRunner(provider=object())
        assert await runner.run_all([]) == []


# ---------------------------------------------------------------------------
# TeamBlueprint
# ---------------------------------------------------------------------------


class TestBlueprint:
    def test_valid(self):
        assert _blueprint().name == "deep_research"

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            TeamBlueprint(
                name="",
                lead_prompt="l",
                worker=WorkerRole(system_prompt="w"),
                synthesis_prompt="s",
            )

    def test_bad_max_workers_rejected(self):
        with pytest.raises(ValueError):
            TeamBlueprint(
                name="x",
                lead_prompt="l",
                worker=WorkerRole(system_prompt="w"),
                synthesis_prompt="s",
                max_workers=0,
            )

    def test_frozen(self):
        bp = _blueprint()
        with pytest.raises(Exception):
            bp.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OrchestratorWorkers — pure helpers
# ---------------------------------------------------------------------------


class TestParseSubtasks:
    def test_json_array(self):
        assert OrchestratorWorkers._parse_subtasks('["a", "b"]', 5) == ["a", "b"]

    def test_fenced_json(self):
        out = OrchestratorWorkers._parse_subtasks('```json\n["a", "b"]\n```', 5)
        assert out == ["a", "b"]

    def test_cap_applied(self):
        assert OrchestratorWorkers._parse_subtasks('["a","b","c"]', 2) == ["a", "b"]

    def test_bullet_fallback(self):
        out = OrchestratorWorkers._parse_subtasks("- first\n- second", 5)
        assert out == ["first", "second"]

    def test_empty(self):
        assert OrchestratorWorkers._parse_subtasks("", 5) == []


class TestSplitReport:
    def test_tags(self):
        report, summary, sources = OrchestratorWorkers._split_report(
            "<summary>sum</summary>\n<report>rep</report>"
        )
        assert report == "rep"
        assert summary == "sum"
        assert sources == []

    def test_no_tags_fallback(self):
        report, summary, sources = OrchestratorWorkers._split_report("just plain text")
        assert report == "just plain text"
        assert summary.startswith("just plain text")
        assert sources == []


class TestSelectTools:
    def test_excludes_delegate(self):
        parent = ToolRegistry()
        for name in ("websearch", "webfetch", "delegate"):
            parent.register(_StubTool(name))
        sub = OrchestratorWorkers._select_tools(parent, ())
        names = {t.name for t in sub}
        assert names == {"websearch", "webfetch"}

    def test_filters_by_names(self):
        parent = ToolRegistry()
        for name in ("websearch", "webfetch", "read"):
            parent.register(_StubTool(name))
        sub = OrchestratorWorkers._select_tools(parent, ("websearch",))
        assert {t.name for t in sub} == {"websearch"}


# ---------------------------------------------------------------------------
# OrchestratorWorkers — execute
# ---------------------------------------------------------------------------


class TestExecute:
    async def test_happy_path(self):
        core = _FakeCore(
            [
                AgentOutput(content='["sub1", "sub2"]'),      # decompose
                AgentOutput(content="<summary>S</summary><report>R</report>"),  # synthesize
                AgentOutput(content="[]"),                     # detect_gaps → no gaps
            ]
        )
        runner = AsyncMock()
        runner.run_all = AsyncMock(
            return_value=[
                SubAgentResult(success=True, content="c1", task="sub1"),
                SubAgentResult(success=True, content="c2", task="sub2"),
            ]
        )
        topo = OrchestratorWorkers(core, runner)
        res = await topo.execute("topic", _blueprint(), ToolRegistry())
        assert res.subtasks == ["sub1", "sub2"]
        assert res.full_report == "R"
        assert res.summary == "S"
        assert len(res.worker_results) == 2

    async def test_empty_decompose_returns_error(self):
        core = _FakeCore([AgentOutput(content="")])
        runner = AsyncMock()
        topo = OrchestratorWorkers(core, runner)
        res = await topo.execute("topic", _blueprint(), ToolRegistry())
        assert res.error
        assert not res.full_report
        runner.run_all.assert_not_called()

    async def test_partial_failure_still_synthesizes(self):
        core = _FakeCore(
            [
                AgentOutput(content='["a", "b"]'),            # decompose
                AgentOutput(content="<report>R</report>"),    # synthesize
                AgentOutput(content="[]"),                     # detect_gaps → no gaps
            ]
        )
        runner = AsyncMock()
        runner.run_all = AsyncMock(
            return_value=[
                SubAgentResult(success=True, content="ok", task="a"),
                SubAgentResult(success=False, content="", task="b", error="timeout"),
            ]
        )
        topo = OrchestratorWorkers(core, runner)
        res = await topo.execute("topic", _blueprint(), ToolRegistry())
        assert res.full_report == "R"
        assert sum(1 for r in res.worker_results if not r.success) == 1

    async def test_refinement_round_fills_gaps(self):
        """When the lead finds gaps after round 1, a second fan-out runs."""
        core = _FakeCore(
            [
                AgentOutput(content='["a", "b"]'),            # decompose R1
                AgentOutput(content="<summary>S</summary><report>R1</report>"),  # synthesize R1
                AgentOutput(content='["gap1"]'),              # detect_gaps → 1 gap
                AgentOutput(content="<summary>S</summary><report>R2</report>"),  # synthesize R2
                AgentOutput(content="[]"),                     # detect_gaps → no more
            ]
        )
        runner = AsyncMock()
        runner.run_all = AsyncMock(side_effect=[
            # Round 1 workers
            [
                SubAgentResult(success=True, content="c1", task="a"),
                SubAgentResult(success=True, content="c2", task="b"),
            ],
            # Round 2 (refinement) workers
            [SubAgentResult(success=True, content="gap_content", task="gap1")],
        ])
        topo = OrchestratorWorkers(core, runner)
        res = await topo.execute("topic", _blueprint(), ToolRegistry())
        assert res.full_report == "R2"
        assert len(res.subtasks) == 3  # ["a", "b", "gap1"]
        assert len(res.worker_results) == 3
        assert runner.run_all.call_count == 2


# ---------------------------------------------------------------------------
# DeepResearchAgent
# ---------------------------------------------------------------------------


class TestDeepResearchAgent:
    async def test_run_archives_report_and_returns_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agents.deep_research_agent.Config.workspace", str(tmp_path)
        )
        agent = DeepResearchAgent(_FakeCore([]))
        team = TeamResult(
            full_report="FULL REPORT BODY",
            summary="SUM",
            subtasks=["a"],
            worker_results=[SubAgentResult(success=True, content="c", task="a")],
        )
        with patch.object(
            OrchestratorWorkers, "execute", AsyncMock(return_value=team)
        ):
            spec = AgentInput(
                init_messages=[{"role": "user", "content": "/research AI agents"}],
                tools=ToolRegistry(),
            )
            out = await agent.run(spec)
        assert "SUM" in out.content
        assert "AI agents" in out.content
        files = list((tmp_path / "research").glob("*.md"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == "FULL REPORT BODY"

    async def test_topic_from_goal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agents.deep_research_agent.Config.workspace", str(tmp_path)
        )
        agent = DeepResearchAgent(_FakeCore([]))
        team = TeamResult(full_report="R", summary="S")
        with patch.object(
            OrchestratorWorkers, "execute", AsyncMock(return_value=team)
        ) as ex:
            spec = AgentInput(
                init_messages=[], tools=ToolRegistry(), goal="quantum computing"
            )
            await agent.run(spec)
        assert ex.call_args.args[0] == "quantum computing"

    async def test_failure_when_no_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agents.deep_research_agent.Config.workspace", str(tmp_path)
        )
        agent = DeepResearchAgent(_FakeCore([]))
        team = TeamResult(full_report="", summary="", error="no subtasks")
        with patch.object(
            OrchestratorWorkers, "execute", AsyncMock(return_value=team)
        ):
            spec = AgentInput(
                init_messages=[{"role": "user", "content": "/research x"}],
                tools=ToolRegistry(),
            )
            out = await agent.run(spec)
        assert out.stop_reason == "error"
        assert "no subtasks" in out.error


# ---------------------------------------------------------------------------
# /research routing
# ---------------------------------------------------------------------------


class TestResearchRouting:
    def test_explicit_match(self):
        assert _match_explicit_command("/research a topic") == "deep_research"

    async def test_dispatcher_resolves(self):
        dispatcher = Dispatcher(
            agents={"react": object(), "deep_research": object()}  # type: ignore[dict-item]
        )
        assert await dispatcher.resolve("/research AI 2026") == "deep_research"

    async def test_plain_input_not_research(self):
        dispatcher = Dispatcher(
            agents={"react": object(), "deep_research": object()}  # type: ignore[dict-item]
        )
        assert await dispatcher.resolve("你好") == "react"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
