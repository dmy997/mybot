"""GAIA dataset loader.

Loads from HuggingFace (``gaia-benchmark/GAIA``) or a local directory.
Requires ``HF_TOKEN`` and ``pip install huggingface-hub``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


class GAIALoader:
    """Load the GAIA dataset from HuggingFace or a local directory.

    Parameters
    ----------
    local_data_dir:
        Path to a local GAIA clone (contains ``2023/validation/metadata.jsonl``).
        If unset, downloads from HuggingFace.
    split:
        ``validation`` (165 samples) or ``test`` (301 samples).
    """

    def __init__(
        self,
        local_data_dir: str | None = None,
        split: str = "validation",
    ):
        self.local_data_dir = Path(local_data_dir) if local_data_dir else None
        self.split = split
        self._data: list[dict[str, Any]] | None = None

    def load(self, level: int | None = None, max_samples: int = 0) -> list[dict[str, Any]]:
        """Load GAIA samples, optionally filtered by *level* (1, 2, or 3)."""
        if self._data is None:
            self._data = self._do_load()

        items = list(self._data)
        if level is not None:
            items = [it for it in items if it.get("level") == level]

        if max_samples and max_samples > 0:
            return items[:max_samples]
        return items

    def _do_load(self) -> list[dict[str, Any]]:
        """Load from local dir or HuggingFace."""
        if self.local_data_dir:
            return self._load_from_local()

        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            raise ImportError(
                "huggingface-hub is required to download GAIA. "
                "Install it with: pip install huggingface-hub"
            )

        cache_dir = Path("data/gaia")
        logger.info("Downloading GAIA dataset from HuggingFace...")
        try:
            local_dir = snapshot_download(
                repo_id="gaia-benchmark/GAIA",
                repo_type="dataset",
                local_dir=str(cache_dir),
            )
        except Exception as exc:
            if "403" in str(exc) or "GatedRepo" in type(exc).__name__:
                raise PermissionError(
                    f"GAIA is a gated dataset. Request access at "
                    f"https://huggingface.co/datasets/gaia-benchmark/GAIA\n"
                    f"Then set HF_TOKEN in .env and retry.\n"
                    f"Or download manually and pass --gaia-data-dir <path>\n"
                    f"Original error: {exc}"
                ) from exc
            raise
        self.local_data_dir = Path(local_dir)
        return self._load_from_local()

    def _load_from_local(self) -> list[dict[str, Any]]:
        """Load samples from local directory (parquet or jsonl metadata)."""
        base = self.local_data_dir / "2023" / self.split

        # Try parquet first (current GAIA format)
        parquet_path = base / "metadata.parquet"
        if parquet_path.exists():
            return self._load_from_parquet(parquet_path)

        # Fallback: JSONL (older GAIA format)
        jsonl_path = base / "metadata.jsonl"
        if jsonl_path.exists():
            return self._load_from_jsonl(jsonl_path)

        raise FileNotFoundError(
            f"GAIA metadata not found in {base}\n"
            f"Download from https://huggingface.co/datasets/gaia-benchmark/GAIA"
        )

    def _load_from_parquet(self, path: Path) -> list[dict[str, Any]]:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError(
                "pyarrow is required to read GAIA parquet data. "
                "Install it with: pip install pyarrow"
            )
        table = pq.read_table(str(path))
        items: list[dict[str, Any]] = []
        for row in table.to_pylist():
            items.append(self._standardize(row))
        logger.info("GAIA loaded {} samples from {}", len(items), path)
        return items

    def _load_from_jsonl(self, path: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(self._standardize(json.loads(line)))
        logger.info("GAIA loaded {} samples from {}", len(items), path)
        return items

    @staticmethod
    def _standardize(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": raw.get("task_id", raw.get("id", "")),
            "question": raw.get("Question", raw.get("question", "")),
            "level": int(raw.get("Level", raw.get("level", 1))),
            "final_answer": raw.get("Final answer", raw.get("final_answer", "")),
            "file_name": raw.get("file_name", ""),
            "file_path": raw.get("file_path", ""),
            "annotator_steps": raw.get("Annotator Metadata", {}).get("Steps", []),
            "_raw": raw,
        }

    def get_statistics(self) -> dict[str, Any]:
        """Return dataset statistics."""
        if self._data is None:
            self._data = self._do_load()
        levels: dict[int, int] = {}
        for item in self._data:
            lvl = item.get("level", 0)
            levels[lvl] = levels.get(lvl, 0) + 1
        return {
            "total_samples": len(self._data),
            "level_distribution": dict(sorted(levels.items())),
            "split": self.split,
        }
