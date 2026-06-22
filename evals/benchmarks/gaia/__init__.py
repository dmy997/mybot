"""GAIA (General AI Assistants) benchmark integration."""

from evals.benchmarks.gaia.dataset import GAIALoader
from evals.benchmarks.gaia.evaluator import GAIAEvaluator
from evals.benchmarks.gaia.metrics import GAIAMetrics, compute_gaia_metrics

__all__ = ["GAIALoader", "GAIAEvaluator", "GAIAMetrics", "compute_gaia_metrics"]
