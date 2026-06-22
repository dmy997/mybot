"""GAIA evaluator — runs agent against GAIA tasks using quasi-exact match."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from evals.benchmarks.gaia.dataset import GAIALoader
from evals.benchmarks.gaia.metrics import GAIAMetrics


class GAIAEvaluator:
    """Evaluate an agent against the GAIA benchmark.

    Parameters
    ----------
    data_dir:
        Optional local path to GAIA dataset.  Downloads from HuggingFace if unset.
    split:
        ``validation`` or ``test``.
    """

    def __init__(self, data_dir: str | None = None, split: str = "validation"):
        self.loader = GAIALoader(local_data_dir=data_dir, split=split)
        self.metrics = GAIAMetrics()

    # -- public API -----------------------------------------------------------

    async def evaluate(
        self,
        agent_factory,
        level: int | None = None,
        max_samples: int = 0,
    ) -> dict[str, Any]:
        """Run GAIA evaluation.

        Parameters
        ----------
        agent_factory:
            An async callable ``f() -> agent`` that creates an agent.
        level:
            Filter by difficulty level (1, 2, 3).  None = all levels.
        max_samples:
            Cap the number of samples (0 = all).
        """
        samples = self.loader.load(level=level, max_samples=max_samples)
        if not samples:
            raise ValueError(f"No GAIA samples loaded (level={level})")

        results: list[dict[str, Any]] = []
        total = len(samples)

        for i, sample in enumerate(samples):
            logger.debug("GAIA {}/{}: {}", i + 1, total, sample.get("task_id", "?"))
            try:
                agent = agent_factory()
                response = await agent.run(sample.get("question", ""))
                predicted = self._extract_answer(response)
                exact, partial = self._quasi_exact_match(
                    predicted, sample.get("final_answer", "")
                )
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "GAIA sample {} failed: {}", sample.get("task_id"), exc
                )
                predicted = ""
                exact, partial = False, False

            results.append({
                "task_id": sample["task_id"],
                "level": sample.get("level", 0),
                "predicted": predicted,
                "expected": sample.get("final_answer", ""),
                "exact_match": exact,
                "partial_match": partial or exact,
            })

        return {
            "results": results,
            "metrics": self.metrics.compute(results),
            "level_filter": level,
            "total_samples": len(results),
        }

    # -- answer extraction / matching ----------------------------------------

    @staticmethod
    def _extract_answer(response: Any) -> str:
        """Extract the final answer from an agent response."""
        text = ""
        if hasattr(response, "content"):
            text = response.content or ""
        elif isinstance(response, dict):
            text = response.get("content", "") or str(response)
        else:
            text = str(response)

        # GAIA format: FINAL ANSWER: [answer]
        m = re.search(r'FINAL\s+ANSWER\s*:\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip('[]')

        # Fallbacks
        for pattern in [
            r'答案\s*[：:]\s*(.+)',
            r'Answer\s*[：:]\s*(.+)',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        # Last non-empty line
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[-1] if lines else text.strip()

    @staticmethod
    def _quasi_exact_match(predicted: str, expected: str) -> tuple[bool, bool]:
        """Return (exact_match, partial_match) after normalization."""
        npred = GAIAEvaluator._normalize(predicted)
        nexpected = GAIAEvaluator._normalize(expected)
        exact = npred == nexpected

        # Partial: one contains the other
        partial = (len(npred) > 2 and npred in nexpected) or (
            len(nexpected) > 2 and nexpected in npred
        )
        return exact, partial

    @staticmethod
    def _normalize(answer: str) -> str:
        """GAIA-style answer normalization."""
        if not answer:
            return ""

        answer = answer.strip().lower()

        # First pass: remove number-formatting commas (between digits)
        answer = re.sub(r"(\d),(\d)", r"\1\2", answer)
        # Remove currency symbols
        answer = answer.replace("$", "").replace("%", "").replace("€", "").replace("£", "")

        # Comma-separated list → sort (only if commas remain after number fix)
        if "," in answer:
            parts = [GAIAEvaluator._normalize_single(p.strip()) for p in answer.split(",")]
            parts.sort()
            return ",".join(parts)

        return GAIAEvaluator._normalize_single(answer)

    @staticmethod
    def _normalize_single(text: str) -> str:
        """Normalize a single answer value."""
        text = text.strip().lower()

        # Remove leading articles
        text = re.sub(r"^(the|a|an)\s+", "", text)

        # Remove currency symbols and percent
        text = text.replace("$", "").replace("%", "").replace("€", "").replace("£", "")

        # Remove commas in numbers: 1,234 → 1234
        text = re.sub(r"(\d),(\d)", r"\1\2", text)

        # Collapse whitespace
        text = " ".join(text.split())

        # Strip trailing punctuation
        text = text.rstrip(".,;:!?")

        return text

    # -- export ---------------------------------------------------------------

    @staticmethod
    def export_to_gaia_format(
        results: list[dict[str, Any]],
        output_path: str,
        include_reasoning: bool = False,
    ) -> None:
        """Export results in GAIA JSONL format."""
        from pathlib import Path

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                entry = {
                    "task_id": r["task_id"],
                    "model_answer": r.get("predicted", ""),
                }
                if include_reasoning:
                    entry["reasoning_trace"] = r.get("reasoning", r.get("predicted", ""))
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("GAIA results exported to {}", output_path)

import json
