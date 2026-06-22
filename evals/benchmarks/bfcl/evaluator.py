"""BFCL evaluator — runs agent predictions against BFCL samples using AST matching."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from loguru import logger

from evals.benchmarks.bfcl.dataset import BFCLLoader
from evals.benchmarks.bfcl.metrics import BFCLMetrics


class BFCLEvaluator:
    """Evaluate an agent against the BFCL benchmark.

    Parameters
    ----------
    data_dir:
        Path to ``bfcl_eval/data/`` in the gorilla repository.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.loader = BFCLLoader(data_dir)
        self.metrics = BFCLMetrics()

    # -- public API -----------------------------------------------------------

    async def evaluate(
        self,
        agent_factory,
        category: str = "simple_python",
        max_samples: int = 0,
    ) -> dict[str, Any]:
        """Run BFCL evaluation.

        Parameters
        ----------
        agent_factory:
            An async callable ``f(tools: list[dict]) -> agent`` that creates
            an agent with the given function definitions registered as tools.
        category:
            BFCL category name.
        max_samples:
            Cap the number of samples (0 = all).
        """
        samples = self.loader.load(category, max_samples=max_samples)
        if not samples:
            raise ValueError(f"No samples loaded for category {category!r}")

        results: list[dict[str, Any]] = []
        total = len(samples)

        for i, sample in enumerate(samples):
            logger.debug("BFCL {}/{}: {}", i + 1, total, sample.get("id", "?"))
            try:
                agent = agent_factory(sample.get("functions", []))
                response = await agent.run(sample.get("question", ""))
                predicted = self._extract_calls(response)
                correct = self._ast_match(predicted, sample.get("ground_truth", []))
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "BFCL sample {} failed: {}", sample.get("id"), exc
                )
                predicted = []
                correct = False

            results.append({
                "id": sample["id"],
                "category": category,
                "question": sample.get("question", ""),
                "predicted": predicted,
                "ground_truth": sample.get("ground_truth", []),
                "correct": correct,
            })

        return {
            "results": results,
            "metrics": self.metrics.compute(results),
            "category": category,
            "total_samples": len(results),
        }

    # -- extraction --------------------------------------------------

    @staticmethod
    def _extract_calls(response: Any) -> list[dict[str, Any]]:
        """Extract function calls from an agent response.

        Handles AgentOutput objects, dicts, and raw strings.
        """
        text = ""
        if hasattr(response, "content"):
            text = response.content or ""
        elif isinstance(response, dict):
            text = response.get("content", "") or str(response)
        else:
            text = str(response)

        # Try JSON array/object first
        calls: list[dict[str, Any]] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                calls = parsed
            elif isinstance(parsed, dict) and "name" in parsed:
                calls = [parsed]
            if calls:
                return BFCLEvaluator._normalize_calls(calls)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try extracting JSON from code blocks
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if m:
            try:
                calls = json.loads(m.group(1))
                if calls:
                    return BFCLEvaluator._normalize_calls(calls)
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: regex for "name": "xxx" / "arguments": {...}
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', text)
        if names:
            return [{"name": n, "arguments": {}} for n in names]

        return []

    @staticmethod
    def _normalize_calls(calls: list[dict]) -> list[dict[str, Any]]:
        """Ensure each call has ``name`` and ``arguments`` keys."""
        out = []
        for c in calls:
            out.append({
                "name": c.get("name", c.get("function_name", "")),
                "arguments": c.get("arguments", c.get("parameters", {})),
            })
        return out

    # -- AST matching -------------------------------------------------

    @staticmethod
    def _ast_match(
        predicted: list[dict[str, Any]],
        ground_truth: list[dict[str, Any]],
    ) -> bool:
        """Compare predicted function calls with ground truth using AST matching.

        Uses set-based matching (order independent) for multi-call scenarios.
        """
        if len(predicted) != len(ground_truth):
            return False

        # Normalize both sides
        pred_asts = {BFCLEvaluator._to_ast_key(c) for c in predicted}
        truth_asts = {BFCLEvaluator._to_ast_key(c) for c in ground_truth}
        return pred_asts == truth_asts

    @staticmethod
    def _to_ast_key(call: dict[str, Any]) -> tuple:
        """Create a hashable key from a function call dict.

        Tries to evaluate argument values so ``2+3`` and ``5`` match.
        """
        name = call.get("name", "")
        args = call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}

        normalized = {}
        for k, v in sorted(args.items()):
            normalized[k] = BFCLEvaluator._safe_eval(v)

        return (name, tuple(sorted(normalized.items())))

    @staticmethod
    def _safe_eval(value: Any) -> Any:
        """Try to evaluate a value as a Python literal for AST comparison."""
        if isinstance(value, str):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value
        return value

    # -- export -------------------------------------------------------

    @staticmethod
    def export_to_bfcl_format(
        results: list[dict[str, Any]],
        output_path: str,
    ) -> None:
        """Export results in BFCL-compatible JSONL format."""
        from pathlib import Path

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                entry = {
                    "id": r["id"],
                    "result": r.get("predicted", []),
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("BFCL results exported to {}", output_path)
