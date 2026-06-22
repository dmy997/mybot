"""mybot Agent evaluation system.

Two-layer design:

Layer 1 — Custom task evaluation (YAML-defined, rule-based scoring, fast)
    ``pytest evals/ -v`` or ``python -m evals``

Layer 2 — Community benchmark integration (BFCL, GAIA)
    ``python -m evals --benchmark bfcl --category simple_python``
    ``python -m evals --benchmark gaia --level 1``
"""

from evals.scorers import (
    CompletionScorer,
    CompositeScorer,
    KeywordScorer,
    LLMJudgeScorer,
    Scorer,
    StepEfficiencyScorer,
    ToolSetScorer,
)
from evals.runner import EvalResult, EvalTask, run_task, run_suite
from evals.reporter import MarkdownReporter, TerminalReporter, to_json

__all__ = [
    # runner
    "EvalTask",
    "EvalResult",
    "run_task",
    "run_suite",
    # scorers
    "Scorer",
    "KeywordScorer",
    "ToolSetScorer",
    "StepEfficiencyScorer",
    "CompletionScorer",
    "LLMJudgeScorer",
    "CompositeScorer",
    # reporter
    "TerminalReporter",
    "MarkdownReporter",
    "to_json",
]
