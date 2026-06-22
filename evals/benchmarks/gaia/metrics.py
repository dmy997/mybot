"""GAIA metrics — exact match rate, level-wise accuracy, drop rate."""

from __future__ import annotations

from typing import Any


class GAIAMetrics:
    """Compute GAIA evaluation metrics."""

    @staticmethod
    def compute(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute all GAIA metrics from per-sample results.

        Each result dict: ``task_id``, ``level``, ``exact_match`` (bool),
        ``partial_match`` (bool).
        """
        total = len(results)
        exact = sum(1 for r in results if r.get("exact_match"))
        partial = sum(1 for r in results if r.get("partial_match"))

        # Level breakdown
        by_level: dict[int, dict[str, int]] = {}
        for r in results:
            lvl = r.get("level", 0)
            if lvl not in by_level:
                by_level[lvl] = {"total": 0, "exact": 0, "partial": 0}
            by_level[lvl]["total"] += 1
            if r.get("exact_match"):
                by_level[lvl]["exact"] += 1
            if r.get("partial_match"):
                by_level[lvl]["partial"] += 1

        level_accuracy: dict[str, float] = {}
        for lvl, stats in sorted(by_level.items()):
            t = stats["total"]
            level_accuracy[f"level_{lvl}"] = round(stats["exact"] / t, 4) if t else 0.0

        # Drop rates
        drop_rates: dict[str, float] = {}
        sorted_levels = sorted(by_level.keys())
        for i in range(len(sorted_levels) - 1):
            a = sorted_levels[i]
            b = sorted_levels[i + 1]
            acc_a = level_accuracy.get(f"level_{a}", 0)
            acc_b = level_accuracy.get(f"level_{b}", 0)
            if acc_a > 0:
                drop_rates[f"drop_{a}_to_{b}"] = round((acc_a - acc_b) / acc_a, 4)

        return {
            "total_samples": total,
            "exact_matches": exact,
            "partial_matches": partial,
            "exact_match_rate": round(exact / total, 4) if total else 0.0,
            "partial_match_rate": round(partial / total, 4) if total else 0.0,
            "level_accuracy": level_accuracy,
            "drop_rates": drop_rates,
            "level_counts": {
                f"level_{lvl}": {"total": s["total"], "exact": s["exact"]}
                for lvl, s in sorted(by_level.items())
            },
        }


def compute_gaia_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Convenience function — see :class:`GAIAMetrics`."""
    return GAIAMetrics.compute(results)
