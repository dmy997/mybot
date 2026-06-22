"""Tests for the evaluation system."""

from __future__ import annotations

import pytest

from evals.runner import EvalResult, discover_tasks
from evals.scorers import ScoreResult


def _all_tasks():
    return discover_tasks()


@pytest.mark.parametrize("eval_task", _all_tasks(), ids=lambda t: t.id)
def test_task_loaded(eval_task):
    """Every task YAML should load with required fields."""
    assert eval_task.id, f"Missing id"
    assert eval_task.prompt, f"Missing prompt in {eval_task.id}"
    assert len(eval_task.prompt) > 10, f"Prompt too short in {eval_task.id}"


@pytest.mark.parametrize("eval_task", _all_tasks(), ids=lambda t: t.id)
def test_task_scoring_pipeline(eval_task):
    """Verify the scoring pipeline works with a synthetic output."""
    from evals.scorers import (
        CompletionScorer,
        CompositeScorer,
        KeywordScorer,
        StepEfficiencyScorer,
        ToolSetScorer,
        compute_overall,
    )

    synthetic_output = {
        "content": " ".join(eval_task.expected_in_answer) + " done",
        "tools_used": eval_task.expected_tools[:2] if eval_task.expected_tools else ["grep"],
        "tool_events": [{"name": t, "ok": True} for t in eval_task.expected_tools],
        "step_count": 3,
        "stop_reason": "completed",
        "error": None,
        "duration": 1.5,
    }

    scorers = CompositeScorer(
        [CompletionScorer(), KeywordScorer(), ToolSetScorer(), StepEfficiencyScorer()],
        weights=[1.0, 1.0, 1.0, 0.5],
    )
    scores = scorers.score(eval_task._raw, synthetic_output)
    overall = compute_overall(scores)

    assert len(scores) == 4
    assert 0.0 <= overall <= 1.0
    assert scores[0].passed


def test_discover_all_categories():
    """Each category subdirectory should have at least one task."""
    tasks = _all_tasks()
    cats = {t.category for t in tasks}
    assert "tool_use" in cats
    assert "reasoning" in cats
    assert "robustness" in cats


def test_result_dataclass():
    """EvalResult should serialize cleanly."""
    r = EvalResult(
        task_id="test",
        category="tool_use",
        paradigm="react",
        passed=True,
        overall_score=0.85,
        scores=[ScoreResult("comp", 1.0, True, "ok")],
    )
    assert r.passed is True
    assert r.overall_score == 0.85


def test_reporter_output():
    """Terminal and Markdown reporters should produce non-empty output."""
    from evals.reporter import MarkdownReporter, TerminalReporter

    results = [
        EvalResult(
            task_id="test1",
            category="tool_use",
            paradigm="react",
            passed=True,
            overall_score=0.9,
            scores=[ScoreResult("comp", 1.0, True, "ok")],
            tools_used=["grep"],
            step_count=3,
            duration_seconds=2.0,
        ),
        EvalResult(
            task_id="test2",
            category="reasoning",
            paradigm="plan_solve",
            passed=False,
            overall_score=0.3,
            scores=[ScoreResult("comp", 0.0, False, "error: timeout")],
            error="timeout",
        ),
    ]

    term = TerminalReporter.render(results)
    assert "Evaluation Results" in term
    assert "test1" in term
    assert "PASS" in term
    assert "FAIL" in term

    md = MarkdownReporter.render(results)
    assert "# Agent Evaluation Report" in md
    assert "test1" in md
    assert "test2" in md
    assert "By Category" in md
    assert "Failures" in md


# ---------------------------------------------------------------------------
# Benchmark unit tests (no live LLM required)
# ---------------------------------------------------------------------------


class TestBFCLMetrics:
    """BFCL metrics computation."""

    def test_bfcl_metrics_all_correct(self):
        from evals.benchmarks.bfcl.metrics import compute_bfcl_metrics

        results = [
            {"id": "s1", "category": "simple", "correct": True},
            {"id": "s2", "category": "simple", "correct": True},
            {"id": "s3", "category": "multiple", "correct": True},
        ]
        m = compute_bfcl_metrics(results)
        assert m["overall_accuracy"] == 1.0
        assert m["correct_samples"] == 3
        assert m["error_rate"] == 0.0

    def test_bfcl_metrics_mixed(self):
        from evals.benchmarks.bfcl.metrics import compute_bfcl_metrics

        results = [
            {"id": "s1", "category": "simple", "correct": True},
            {"id": "s2", "category": "simple", "correct": False},
            {"id": "s3", "category": "multiple", "correct": True},
            {"id": "s4", "category": "multiple", "correct": False},
        ]
        m = compute_bfcl_metrics(results)
        assert m["overall_accuracy"] == 0.5
        assert m["category_accuracy"]["simple"] == 0.5
        assert m["category_accuracy"]["multiple"] == 0.5

    def test_bfcl_metrics_empty(self):
        from evals.benchmarks.bfcl.metrics import compute_bfcl_metrics

        m = compute_bfcl_metrics([])
        assert m["total_samples"] == 0
        assert m["overall_accuracy"] == 0.0


class TestGAIAMetrics:
    """GAIA metrics computation."""

    def test_gaia_metrics_all_exact(self):
        from evals.benchmarks.gaia.metrics import compute_gaia_metrics

        results = [
            {"task_id": "g1", "level": 1, "exact_match": True, "partial_match": True},
            {"task_id": "g2", "level": 2, "exact_match": True, "partial_match": True},
        ]
        m = compute_gaia_metrics(results)
        assert m["exact_match_rate"] == 1.0
        assert m["exact_matches"] == 2

    def test_gaia_metrics_drop_rate(self):
        from evals.benchmarks.gaia.metrics import compute_gaia_metrics

        results = [
            {"task_id": "g1", "level": 1, "exact_match": True, "partial_match": True},
            {"task_id": "g2", "level": 1, "exact_match": True, "partial_match": True},
            {"task_id": "g3", "level": 2, "exact_match": True, "partial_match": True},
            {"task_id": "g4", "level": 2, "exact_match": False, "partial_match": True},
        ]
        m = compute_gaia_metrics(results)
        assert m["level_accuracy"]["level_1"] == 1.0
        assert m["level_accuracy"]["level_2"] == 0.5
        assert m["drop_rates"]["drop_1_to_2"] == 0.5

    def test_gaia_metrics_empty(self):
        from evals.benchmarks.gaia.metrics import compute_gaia_metrics

        m = compute_gaia_metrics([])
        assert m["total_samples"] == 0


class TestBFCLNormalize:
    """BFCL answer normalization."""

    def test_ast_match_same_name(self):
        from evals.benchmarks.bfcl.evaluator import BFCLEvaluator

        assert BFCLEvaluator._ast_match(
            [{"name": "get_weather", "arguments": {"city": "Beijing"}}],
            [{"name": "get_weather", "arguments": {"city": "Beijing"}}],
        )

    def test_ast_match_different_name(self):
        from evals.benchmarks.bfcl.evaluator import BFCLEvaluator

        assert not BFCLEvaluator._ast_match(
            [{"name": "get_temperature", "arguments": {"city": "Beijing"}}],
            [{"name": "get_weather", "arguments": {"city": "Beijing"}}],
        )

    def test_ast_match_empty(self):
        from evals.benchmarks.bfcl.evaluator import BFCLEvaluator

        assert BFCLEvaluator._ast_match([], [])
        assert not BFCLEvaluator._ast_match([{"name": "f", "arguments": {}}], [])


class TestGAIANormalize:
    """GAIA answer normalization."""

    def test_normalize_number(self):
        from evals.benchmarks.gaia.evaluator import GAIAEvaluator

        assert GAIAEvaluator._normalize("$1,234.56") == "1234.56"
        assert GAIAEvaluator._normalize("50%") == "50"

    def test_normalize_string(self):
        from evals.benchmarks.gaia.evaluator import GAIAEvaluator

        assert GAIAEvaluator._normalize("The United States") == "united states"
        assert GAIAEvaluator._normalize("  Hello  World.  ") == "hello world"

    def test_normalize_list(self):
        from evals.benchmarks.gaia.evaluator import GAIAEvaluator

        result = GAIAEvaluator._normalize("Paris, London, Berlin")
        assert "berlin" in result
        assert "london" in result
        assert "paris" in result

    def test_quasi_exact_match(self):
        from evals.benchmarks.gaia.evaluator import GAIAEvaluator

        exact, _ = GAIAEvaluator._quasi_exact_match("42", "42")
        assert exact
        exact, _ = GAIAEvaluator._quasi_exact_match("43", "42")
        assert not exact


class TestLLMJudgeScorer:
    """LLM Judge scorer basic tests."""

    def test_sync_stub(self):
        from evals.scorers import LLMJudgeScorer

        scorer = LLMJudgeScorer(provider=None)
        result = scorer.score(
            {"id": "test", "expected_in_answer": ["hello"]},
            {"content": "hello world", "error": None},
        )
        assert result.name == "llm_judge"
        assert result.passed  # stub always passes


class TestCLI:
    """CLI argument parsing."""

    def test_parser_help(self):
        from evals.__main__ import main as _main
        # just verify the module is importable
        assert True
