"""Evaluation reporters — terminal, JSON, and Markdown output."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from evals.runner import EvalResult


class TerminalReporter:
    """Print eval results as a formatted terminal table."""

    @staticmethod
    def render(results: list[EvalResult]) -> str:
        if not results:
            return "No results."

        lines = [
            "",
            "=" * 72,
            "  Evaluation Results",
            "=" * 72,
            "",
            f"  {'Task':<28s} {'Paradigm':<12s} {'Score':>6s} {'Pass':>5s}  {'Scores'}",
            f"  {'-' * 28} {'-' * 12} {'-' * 6} {'-' * 5}  {'-' * 40}",
        ]

        for r in results:
            score_str = f"{r.overall_score:.2f}"
            pass_str = "PASS" if r.passed else "FAIL"
            dim_str = ", ".join(f"{s.name[:4]}={s.value:.1f}" for s in r.scores)
            lines.append(
                f"  {r.task_id:<28s} {r.paradigm:<12s} {score_str:>6s} {pass_str:>5s}  {dim_str}"
            )

        lines.append("")
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        avg = sum(r.overall_score for r in results) / max(total, 1)
        lines.append(f"  Total: {passed}/{total} passed | Avg score: {avg:.2f}")
        lines.append("=" * 72)
        lines.append("")
        return "\n".join(lines)


class MarkdownReporter:
    """Generate a Markdown eval report."""

    @staticmethod
    def render(results: list[EvalResult], title: str = "Agent Evaluation Report") -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        avg = sum(r.overall_score for r in results) / max(total, 1)

        lines = [
            f"# {title}",
            "",
            f"**Generated**: {ts}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Tasks run | {total} |",
            f"| Passed | {passed} |",
            f"| Pass rate | {passed / max(total, 1):.1%} |",
            f"| Avg score | {avg:.2f} |",
            "",
            "## Per-Task Results",
            "",
            "| Task | Category | Paradigm | Score | Pass | Steps | Time | Details |",
            "|------|----------|----------|-------|------|-------|------|---------|",
        ]

        for r in results:
            pass_icon = "+" if r.passed else "-"
            dim_str = "; ".join(f"{s.name}: {s.value:.1f} ({s.detail})" for s in r.scores)
            lines.append(
                f"| {r.task_id} | {r.category} | {r.paradigm} "
                f"| {r.overall_score:.2f} | {pass_icon} "
                f"| {r.step_count} | {r.duration_seconds:.1f}s "
                f"| {dim_str} |"
            )

        lines.extend(["", "## By Category", ""])
        cats: dict[str, list[EvalResult]] = {}
        for r in results:
            cats.setdefault(r.category, []).append(r)

        lines.append("| Category | Count | Pass Rate | Avg Score |")
        lines.append("|----------|-------|-----------|-----------|")
        for cat, cat_results in sorted(cats.items()):
            cp = sum(1 for r in cat_results if r.passed)
            ca = sum(r.overall_score for r in cat_results) / max(len(cat_results), 1)
            lines.append(f"| {cat} | {len(cat_results)} | {cp}/{len(cat_results)} | {ca:.2f} |")

        lines.extend(["", "## Failures", ""])
        failures = [r for r in results if not r.passed]
        if failures:
            for r in failures:
                lines.append(f"- **{r.task_id}** ({r.paradigm}): score={r.overall_score:.2f}")
                if r.error:
                    lines.append(f"  - error: `{r.error}`")
                for s in r.scores:
                    if not s.passed:
                        lines.append(f"  - {s.name}: {s.detail}")
        else:
            lines.append("No failures.")

        lines.append("")
        return "\n".join(lines)


def to_json(results: list[EvalResult]) -> str:
    """Serialize results as pretty-printed JSON."""
    data: list[dict[str, Any]] = []
    for r in results:
        data.append({
            "task_id": r.task_id,
            "category": r.category,
            "paradigm": r.paradigm,
            "passed": r.passed,
            "overall_score": r.overall_score,
            "scores": [
                {"name": s.name, "value": s.value, "passed": s.passed, "detail": s.detail}
                for s in r.scores
            ],
            "tools_used": r.tools_used,
            "step_count": r.step_count,
            "duration_seconds": r.duration_seconds,
            "error": r.error,
        })
    return json.dumps(data, indent=2, ensure_ascii=False)
