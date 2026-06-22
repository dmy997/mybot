"""BFCL metrics — accuracy, category breakdown, AST match rate."""

from __future__ import annotations

from typing import Any


class BFCLMetrics:
    """Compute BFCL evaluation metrics."""

    @staticmethod
    def compute(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute all metrics from a list of per-sample results.

        Each result dict should have: ``id``, ``category``, ``correct`` (bool).
        """
        total = len(results)
        correct = sum(1 for r in results if r.get("correct"))
        accuracy = correct / total if total > 0 else 0.0

        # Category breakdown
        by_cat: dict[str, dict[str, int]] = {}
        for r in results:
            cat = r.get("category", "unknown")
            if cat not in by_cat:
                by_cat[cat] = {"total": 0, "correct": 0}
            by_cat[cat]["total"] += 1
            if r.get("correct"):
                by_cat[cat]["correct"] += 1

        cat_accuracy = {
            cat: (stats["correct"] / stats["total"] if stats["total"] else 0.0)
            for cat, stats in by_cat.items()
        }

        return {
            "total_samples": total,
            "correct_samples": correct,
            "overall_accuracy": round(accuracy, 4),
            "ast_match_rate": round(accuracy, 4),
            "category_accuracy": cat_accuracy,
            "error_rate": round(1.0 - accuracy, 4),
            "category_counts": {
                cat: {"total": s["total"], "correct": s["correct"]}
                for cat, s in by_cat.items()
            },
        }


def compute_bfcl_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Convenience function — see :class:`BFCLMetrics`."""
    return BFCLMetrics.compute(results)
