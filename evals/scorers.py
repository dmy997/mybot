"""Evaluation scorers — rule-based and LLM-judge scoring for agent outputs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ScoreResult:
    """Single scorer output."""

    name: str
    value: float  # 0.0 — 1.0
    passed: bool
    detail: str = ""


class Scorer(ABC):
    """Base class for all scorers."""

    name: str = "base"

    @abstractmethod
    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        """Score agent output against task expectations."""
        ...


# ---------------------------------------------------------------------------
# Rule-based scorers
# ---------------------------------------------------------------------------


class CompletionScorer(Scorer):
    """Score whether the agent completed successfully (no error)."""

    name = "completion"

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        error = output.get("error") or output.get("stop_reason") == "error"
        if error:
            return ScoreResult(self.name, 0.0, False, f"error: {output.get('error', 'unknown')}")
        return ScoreResult(self.name, 1.0, True, "completed")


class KeywordScorer(Scorer):
    """Check if expected keywords appear in the agent's final answer.

    Task field: ``expected_in_answer: [list, of, keywords]``
    """

    name = "keyword_match"

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        expected = task.get("expected_in_answer", [])
        if not expected:
            return ScoreResult(self.name, 1.0, True, "no keywords specified — skipped")

        content = str(output.get("content", "")).lower()
        tools_used = str(output.get("tools_used", [])).lower()
        search_text = content + " " + tools_used
        for event in output.get("tool_events", []):
            search_text += " " + str(event).lower()

        hits = [kw for kw in expected if kw.lower() in search_text]
        missed = [kw for kw in expected if kw.lower() not in search_text]

        ratio = len(hits) / len(expected) if expected else 1.0
        detail = f"hits={hits}"
        if missed:
            detail += f" missed={missed}"
        return ScoreResult(self.name, ratio, ratio >= 0.6, detail)


class ToolSetScorer(Scorer):
    """Jaccard similarity between expected tools and actual tools used.

    Task field: ``expected_tools: [tool_name, ...]``
    """

    name = "tool_accuracy"

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        expected = set(task.get("expected_tools", []))
        if not expected:
            return ScoreResult(self.name, 1.0, True, "no expected tools — skipped")

        actual = set(output.get("tools_used", []))
        intersection = expected & actual
        union = expected | actual

        jaccard = len(intersection) / len(union) if union else 1.0
        missing = expected - actual
        detail = f"used={sorted(actual)}"
        if intersection:
            detail += f" matched={sorted(intersection)}"
        if missing:
            detail += f" missing={sorted(missing)}"
        return ScoreResult(self.name, jaccard, jaccard >= 0.5, detail)


class StepEfficiencyScorer(Scorer):
    """Score step efficiency: 1 - actual/max, clamped to [0,1].

    Task field: ``max_steps: int``
    """

    name = "step_efficiency"

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        max_steps = task.get("max_steps", 10)
        actual_steps = output.get("step_count", 0)
        if actual_steps <= 0:
            actual_steps = 1

        ratio = min(actual_steps / max_steps, 1.0)
        score = 1.0 - ratio
        detail = f"{actual_steps}/{max_steps} steps (ratio={ratio:.2f})"
        return ScoreResult(self.name, score, actual_steps <= max_steps, detail)


class CompositeScorer:
    """Weighted combination of multiple scorers."""

    def __init__(self, scorers: list[Scorer], weights: list[float] | None = None):
        self.scorers = scorers
        if weights is None:
            weights = [1.0] * len(scorers)
        total = sum(weights)
        self.weights = [w / total for w in weights]

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> list[ScoreResult]:
        results: list[ScoreResult] = []
        for scorer in self.scorers:
            try:
                results.append(scorer.score(task, output))
            except Exception as exc:
                results.append(ScoreResult(scorer.name, 0.0, False, f"scorer error: {exc}"))
        return results


def compute_overall(results: list[ScoreResult], weights: list[float] | None = None) -> float:
    """Compute weighted average from score results."""
    if not results:
        return 0.0
    if weights is None:
        return sum(r.value for r in results) / len(results)
    total_w = sum(weights)
    return sum(r.value * w / total_w for r, w in zip(results, weights))


# ---------------------------------------------------------------------------
# LLM Judge scorer
# ---------------------------------------------------------------------------


class LLMJudgeScorer(Scorer):
    """Use an LLM to judge answer quality on multiple dimensions.

    Requires an LLM provider (the "judge" model — should be a cheap model).

    Dimensions: correctness, completeness, conciseness (all 1-5).
    """

    name = "llm_judge"

    JUDGE_PROMPT = """You are an expert evaluator. Judge the quality of an AI agent's answer.

Task: {task_description}
Expected ideal answer keywords: {expected_keywords}

Agent's answer:
{agent_answer}

Rate the answer on these dimensions (1-5 scale):
1. Correctness: Is the answer factually correct?
2. Completeness: Does it cover all expected aspects?
3. Conciseness: Is it clear and to the point?

Reply with a JSON object only:
{{"correctness": <int>, "completeness": <int>, "conciseness": <int>, "reason": "<one sentence>"}}"""

    def __init__(self, provider: Any, model: str | None = None):
        self.provider = provider
        self.model = model

    async def score_async(
        self, task: dict[str, Any], output: dict[str, Any]
    ) -> ScoreResult:
        """Async version — uses LLM to judge."""
        content = output.get("content", "")
        if not content or output.get("error"):
            return ScoreResult(self.name, 0.0, False, "no content or error")

        prompt = self.JUDGE_PROMPT.format(
            task_description=task.get("description", task.get("id", "")),
            expected_keywords=", ".join(task.get("expected_in_answer", [])),
            agent_answer=content[:4000],
        )

        try:
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )
            import json as _json

            text = response.content or ""
            # Extract JSON
            m = __import__("re").search(r"\{.*\}", text, __import__("re").DOTALL)
            if m:
                data = _json.loads(m.group())
                scores = [
                    data.get("correctness", 0),
                    data.get("completeness", 0),
                    data.get("conciseness", 0),
                ]
                avg = sum(scores) / (len(scores) * 5) if scores else 0.0
                return ScoreResult(
                    self.name,
                    round(avg, 3),
                    avg >= 0.6,
                    data.get("reason", str(scores)),
                )
        except Exception as exc:
            return ScoreResult(self.name, 0.0, False, f"LLM judge error: {exc}")

        return ScoreResult(self.name, 0.0, False, "could not parse judge response")

    def score(self, task: dict[str, Any], output: dict[str, Any]) -> ScoreResult:
        """Sync fallback — use async version when possible."""
        return ScoreResult(self.name, 0.5, True, "sync stub — use score_async() for real judge")
