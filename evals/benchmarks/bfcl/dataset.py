"""BFCL dataset loader.

Loads BFCL v4 data from a local clone of the gorilla repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

_BFCL_CATEGORIES = [
    "simple_python", "simple_java", "simple_javascript",
    "multiple", "parallel", "irrelevance",
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, returning a list of parsed objects."""
    results: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            results.append(json.loads(line))
    return results


class BFCLLoader:
    """Load BFCL v4 dataset from a local clone.

    Parameters
    ----------
    bfcl_data_dir:
        Path to ``bfcl_eval/data/`` in the gorilla repository.
    """

    def __init__(self, bfcl_data_dir: str | Path):
        self.data_dir = Path(bfcl_data_dir)
        self._data: dict[str, list[dict[str, Any]]] = {}

    @property
    def available_categories(self) -> list[str]:
        """Return categories with data files on disk."""
        cats: list[str] = []
        for cat in _BFCL_CATEGORIES:
            fname = f"BFCL_v4_{cat}.json"
            if (self.data_dir / fname).exists():
                cats.append(cat)
        return cats

    def load(self, category: str, max_samples: int = 0) -> list[dict[str, Any]]:
        """Load BFCL samples for *category*.

        Parameters
        ----------
        category:
            One of ``simple_python``, ``multiple``, ``parallel``, ``irrelevance``, etc.
        max_samples:
            Cap the number of samples (0 = all).
        """
        if category in self._data:
            items = self._data[category]
        else:
            fname = f"BFCL_v4_{category}.json"
            path = self.data_dir / fname
            if not path.exists():
                raise FileNotFoundError(
                    f"BFCL data not found: {path}\n"
                    f"Clone https://github.com/ShishirPatil/gorilla.git "
                    f"to get the dataset."
                )
            raw = _read_jsonl(path)
            items = self._standardize(raw, category)
            self._data[category] = items

        if max_samples and max_samples > 0:
            return items[:max_samples]
        return items

    def load_ground_truth(self, category: str) -> list[dict[str, Any]]:
        """Load ground-truth answers from ``possible_answer/``."""
        fname = f"BFCL_v4_{category}.json"
        path = self.data_dir / "possible_answer" / fname
        if not path.exists():
            logger.warning("Ground truth file not found: {}", path)
            return []
        items = _read_jsonl(path)
        if not items:
            return []
        # per-entry format: {"id": ..., "ground_truth": [...]}
        return [item.get("ground_truth", []) for item in items]

    @staticmethod
    def _standardize(raw: list[dict], category: str) -> list[dict[str, Any]]:
        """Normalize BFCL samples to a common format."""
        items: list[dict[str, Any]] = []
        for i, entry in enumerate(raw):
            items.append({
                "id": entry.get("id", f"{category}_{i}"),
                "category": category,
                "question": entry.get("question", entry.get("prompt", "")),
                "functions": entry.get("function", entry.get("functions", [])),
                "ground_truth": entry.get("ground_truth", entry.get("answers", [])),
            })
        return items
