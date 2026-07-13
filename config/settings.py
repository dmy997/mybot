"""Per-model context-window configuration loaded from ``~/.mybot/settings.json``.

Follows the same pattern as Claude Code's ``~/.claude/settings.json``.
All values are optional — hardcoded defaults serve as the ultimate fallback.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Defaults (used when settings.json is missing or keys are absent)
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_WINDOW = 200_000
_DEFAULT_MAX_OUTPUT_TOKENS = 20_000

_DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "warning_buffer_ratio": 0.11,
    "auto_compact_buffer_ratio": 0.072,
    "block_buffer_ratio": 0.017,
    "compress_ratio": 0.5,
    "consolidation_ratio": 0.7,
    "idle_compress_seconds": 300,
    "max_session_messages": 2000,
    "session_ttl_days": 30,
}

_DEFAULT_MODELS: list[dict] = [
    {"pattern": "deepseek/*", "context_window": 200_000, "max_output_tokens": 20_000},
    {"pattern": "gpt-4o*", "context_window": 128_000, "max_output_tokens": 16_384},
    {"pattern": "gpt-4*", "context_window": 128_000, "max_output_tokens": 16_384},
    {"pattern": "claude-*", "context_window": 200_000, "max_output_tokens": 32_000},
    {"pattern": "*", "context_window": 200_000, "max_output_tokens": 20_000},
]

_DEFAULT_ENV: dict[str, str] = {
    "WORKSPACE": "~/.mybot/workspace",
    "PROVIDER_NAME": "openrouter",
    "LLM_MODEL_ID": "deepseek/deepseek-v4-flash",
    "LIGHT_MODEL_NAME": "deepseek/deepseek-v4-flash",
    "MULTIMODAL_MODEL": "openai/gpt-4o-mini",
    "OPENAI_API_KEY": "",
    "OPENAI_API_BASE": "https://openrouter.ai/api/v1",
    "LLM_TIMEOUT": "60",
    "CONTEXT_WINDOW": "200000",
    "MAX_OUTPUT_TOKENS": "20000",
    "WARNING_BUFFER_RATIO": "0.11",
    "AUTOCOMPACT_BUFFER_RATIO": "0.072",
    "BLOCK_BUFFER_RATIO": "0.017",
    "COMPRESS_RATIO": "0.5",
    "CONSOLIDATION_RATIO": "0.7",
    "IDLE_COMPRESS_SECONDS": "300",
    "MAX_SESSION_MESSAGES": "2000",
    "SESSION_TTL_DAYS": "30",
    "MYBOT_HOST": "127.0.0.1",
    "MYBOT_PORT": "8080",
    "MYBOT_API_KEY": "",
    "MYBOT_CHECKPOINT": "",
    "MYBOT_SANDBOX_BACKEND": "none",
    "TAVILY_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GOOGLE_CSE_ID": "",
    "BING_API_KEY": "",
    "XIAOHONGSHU_FALLBACK_CHAT": "filehelper",
    "DUMP_LLM_MESSAGES": "",
    "HYBRID_SEARCH_ENABLED": "true",
    "EMBEDDING_MODEL": "all-MiniLM-L6-v2",
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ModelWindowConfig:
    """Context-window parameters for a specific model."""

    context_window: int = _DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS


@dataclass
class ThresholdsConfig:
    """Token-budget and session threshold ratios."""

    warning_buffer_ratio: float = 0.11
    auto_compact_buffer_ratio: float = 0.072
    block_buffer_ratio: float = 0.017
    compress_ratio: float = 0.5
    consolidation_ratio: float = 0.7
    idle_compress_seconds: int = 300
    max_session_messages: int = 2000
    session_ttl_days: int = 30


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_settings: dict | None = None


def _settings_path() -> Path:
    return Path.home() / ".mybot" / "settings.json"


def get_settings() -> dict:
    """Return the parsed settings dict (lazy singleton, cached after first call)."""
    global _settings
    if _settings is None:
        _settings = _load_settings(_settings_path())
    return _settings


def _load_settings(path: Path) -> dict:
    """Read and parse the JSON settings file.  Returns ``{}`` on any failure."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("{!s} is not a JSON object, ignoring", path)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to parse {!s}: {}", path, exc)
        return {}


def reload_settings() -> dict:
    """Force re-read of settings.json (useful after external edits)."""
    global _settings
    _settings = _load_settings(_settings_path())
    return _settings


def apply_settings_env() -> None:
    """Inject settings.json ``env`` dict into ``os.environ`` (override=False).

    Called early in ``config/config.py`` before ``_load_dotenv()`` so that
    ``Config`` class attributes pick up settings.json values.

    Existing environment variables take priority (override=False).
    """
    settings = get_settings()
    env_vars = settings.get("env")
    if not isinstance(env_vars, dict):
        return
    for key, value in env_vars.items():
        if isinstance(value, str) and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Model lookup
# ---------------------------------------------------------------------------


def lookup_model(model_id: str, models: list[dict] | None = None) -> ModelWindowConfig:
    """Find the first model entry whose ``pattern`` fnmatch-es *model_id*.

    When *models* is ``None``, the ``"models"`` key from ``get_settings()``
    is used.  Falls back to ``_DEFAULT_MODELS`` when the settings file has
    no ``"models"`` key.
    """
    if models is None:
        settings = get_settings()
        models = settings.get("models")
        if not models:
            models = _DEFAULT_MODELS

    for entry in models:
        pattern = entry.get("pattern", "*")
        if fnmatch.fnmatch(model_id, pattern):
            return ModelWindowConfig(
                context_window=int(entry.get("context_window", _DEFAULT_CONTEXT_WINDOW)),
                max_output_tokens=int(entry.get("max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS)),
            )

    return ModelWindowConfig()


def resolve_context_window(model_id: str) -> ModelWindowConfig:
    """Convenience: settings → lookup_model for *model_id*."""
    return lookup_model(model_id)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def load_thresholds(data: dict | None = None) -> ThresholdsConfig:
    """Extract ``thresholds`` from *data* (falls back to ``get_settings()``).

    Missing keys fall back to ``_DEFAULT_THRESHOLDS``.
    """
    if data is None:
        data = get_settings()
    t = data.get("thresholds", {}) if data else {}
    if not isinstance(t, dict):
        t = {}
    return ThresholdsConfig(
        warning_buffer_ratio=float(
            t.get("warning_buffer_ratio", _DEFAULT_THRESHOLDS["warning_buffer_ratio"])
        ),
        auto_compact_buffer_ratio=float(
            t.get("auto_compact_buffer_ratio", _DEFAULT_THRESHOLDS["auto_compact_buffer_ratio"])
        ),
        block_buffer_ratio=float(
            t.get("block_buffer_ratio", _DEFAULT_THRESHOLDS["block_buffer_ratio"])
        ),
        compress_ratio=float(
            t.get("compress_ratio", _DEFAULT_THRESHOLDS["compress_ratio"])
        ),
        consolidation_ratio=float(
            t.get("consolidation_ratio", _DEFAULT_THRESHOLDS["consolidation_ratio"])
        ),
        idle_compress_seconds=int(
            t.get("idle_compress_seconds", _DEFAULT_THRESHOLDS["idle_compress_seconds"])
        ),
        max_session_messages=int(
            t.get("max_session_messages", _DEFAULT_THRESHOLDS["max_session_messages"])
        ),
        session_ttl_days=int(
            t.get("session_ttl_days", _DEFAULT_THRESHOLDS["session_ttl_days"])
        ),
    )


# ---------------------------------------------------------------------------
# Default file generation
# ---------------------------------------------------------------------------


def generate_default_settings(path: Path | None = None) -> Path:
    """Write the default settings.json and return its path.

    Does **not** overwrite an existing file.
    """
    target = path or _settings_path()
    if target.exists():
        return target

    default = {
        "env": _DEFAULT_ENV,
        "models": _DEFAULT_MODELS,
        "thresholds": _DEFAULT_THRESHOLDS,
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(default, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Generated default settings at {!s}", target)
    return target
