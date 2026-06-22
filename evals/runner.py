"""Evaluation runner — loads tasks, executes agents, scores results."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from evals.scorers import (
    CompletionScorer,
    CompositeScorer,
    KeywordScorer,
    ScoreResult,
    StepEfficiencyScorer,
    ToolSetScorer,
    compute_overall,
)

_DEFAULT_SCORERS: CompositeScorer | None = None


def _get_default_scorers() -> CompositeScorer:
    global _DEFAULT_SCORERS
    if _DEFAULT_SCORERS is None:
        _DEFAULT_SCORERS = CompositeScorer(
            [CompletionScorer(), KeywordScorer(), ToolSetScorer(), StepEfficiencyScorer()],
            weights=[1.0, 1.0, 1.0, 0.5],
        )
    return _DEFAULT_SCORERS


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class EvalTask:
    """A single evaluation task loaded from YAML."""

    id: str
    category: str
    description: str
    prompt: str
    expected_tools: list[str] = field(default_factory=list)
    expected_in_answer: list[str] = field(default_factory=list)
    max_steps: int = 10
    timeout_seconds: int = 120
    paradigms: list[str] = field(default_factory=list)
    _raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    """Result from running one task with one paradigm."""

    task_id: str
    category: str
    paradigm: str
    passed: bool
    overall_score: float
    scores: list[ScoreResult] = field(default_factory=list)
    tool_events: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    step_count: int = 0
    content_preview: str = ""
    duration_seconds: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# Task loading
# ---------------------------------------------------------------------------


def discover_tasks(tasks_dir: Path | str | None = None) -> list[EvalTask]:
    """Discover all YAML task files under *tasks_dir*."""
    if tasks_dir is None:
        tasks_dir = Path(__file__).resolve().parent / "tasks"
    tasks_dir = Path(tasks_dir)

    tasks: list[EvalTask] = []
    for yaml_path in sorted(tasks_dir.rglob("*.yaml")):
        try:
            tasks.append(_load_task(yaml_path))
        except Exception:
            logger.opt(exception=True).warning("Failed to load task from {}", yaml_path)
    return tasks


def _load_task(path: Path) -> EvalTask:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    category = path.parent.name
    return EvalTask(
        id=data.get("id", path.stem),
        category=data.get("category", category),
        description=data.get("description", ""),
        prompt=data.get("prompt", ""),
        expected_tools=data.get("expected_tools", []),
        expected_in_answer=data.get("expected_in_answer", []),
        max_steps=data.get("max_steps", 10),
        timeout_seconds=data.get("timeout_seconds", 120),
        paradigms=data.get("paradigms", []),
        _raw=data,
    )


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


async def _run_agent(
    task: EvalTask,
    provider: Any,
    tools: dict[str, Any],
    paradigm: str,
    model: str | None = None,
) -> dict[str, Any]:
    from core.runner import AgentCore, AgentInput
    from tools import ToolRegistry

    registry = ToolRegistry()
    for t in tools.values():
        registry.register(t)

    core = AgentCore(provider=provider, workspace=_get_workspace())

    spec = AgentInput(
        init_messages=[{"role": "user", "content": task.prompt}],
        tools=registry,
        model=model,
        session_key=f"eval-{task.id}-{paradigm}",
        paradigm=paradigm,
        checkpoint=False,
    )

    t0 = time.monotonic()
    try:
        output = await asyncio.wait_for(core.run(spec), timeout=task.timeout_seconds)
        duration = time.monotonic() - t0
        return {
            "content": output.content,
            "tools_used": output.tools_used,
            "tool_events": [
                {"name": e.get("name", ""), "ok": e.get("ok", True)}
                for e in output.tool_events
            ],
            "step_count": _count_steps(output.messages),
            "stop_reason": output.stop_reason,
            "error": output.error,
            "duration": duration,
        }
    except asyncio.TimeoutError:
        duration = time.monotonic() - t0
        return {"error": f"timeout after {task.timeout_seconds}s", "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        logger.opt(exception=True).warning("Agent run failed for task {}", task.id)
        return {"error": str(exc), "duration": duration}


def _count_steps(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))


def _get_workspace() -> Path | None:
    try:
        from config import Config
        return Config.workspace
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_task(
    task: EvalTask,
    provider: Any,
    tools: Any,
    *,
    paradigm: str = "react",
    model: str | None = None,
    scorers: CompositeScorer | None = None,
) -> EvalResult:
    """Run a single task and return the scored result."""
    if scorers is None:
        scorers = _get_default_scorers()

    raw = await _run_agent(task, provider, tools, paradigm, model=model)
    scores = scorers.score(task._raw, raw)
    overall = compute_overall(scores, weights=None)
    error = raw.get("error")
    passed = all(s.passed for s in scores) if not error else False

    return EvalResult(
        task_id=task.id,
        category=task.category,
        paradigm=paradigm,
        passed=passed,
        overall_score=round(overall, 3),
        scores=scores,
        tool_events=raw.get("tool_events", []),
        tools_used=raw.get("tools_used", []),
        step_count=raw.get("step_count", 0),
        content_preview=_truncate(raw.get("content", ""), 200),
        duration_seconds=round(raw.get("duration", 0.0), 2),
        error=error,
    )


async def run_suite(
    tasks: list[EvalTask],
    provider: Any,
    tools: Any,
    *,
    paradigm: str = "react",
    model: str | None = None,
    scorers: CompositeScorer | None = None,
) -> list[EvalResult]:
    """Run a suite of tasks sequentially and return results."""
    results: list[EvalResult] = []
    for task in tasks:
        paradigms = task.paradigms if task.paradigms else [paradigm]
        for p in paradigms:
            logger.info("Running task {!r} with paradigm {!r}", task.id, p)
            result = await run_task(task, provider, tools, paradigm=p, model=model, scorers=scorers)
            results.append(result)
    return results


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
