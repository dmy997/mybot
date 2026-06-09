"""Config — unified configuration loaded from environment / .env file.

All settings are read once at import time via ``python-dotenv``.
Call ``Config.reload()`` to re-read ``.env`` and refresh all values.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_dotenv() -> Path | None:
    """Return the path to the ``.env`` file in the project root, if it exists."""
    env_path = _PROJECT_ROOT / ".env"
    return env_path if env_path.is_file() else None


def _load_dotenv() -> None:
    """Load the project-root ``.env`` into ``os.environ``."""
    path = _find_dotenv()
    if path is not None:
        try:
            from dotenv import load_dotenv

            load_dotenv(path, override=False)
        except ImportError:
            # python-dotenv not installed — env vars must already be set
            pass


# Load .env before Config class reads os.environ
_load_dotenv()


class Config:
    """Typed configuration container for the mybot agent framework.

    Loads values from the environment (after ``.env`` has been injected),
    falling back to sensible defaults.  All fields are class-level attributes.

    Call ``Config.reload()`` to re-read ``.env`` and refresh all values.
    """

    workspace: str = os.getenv("WORKSPACE", "~/.mybot/workspace")

    # ------------------------------------------------------------------
    # Provider
    # ------------------------------------------------------------------

    api_key: str = os.getenv("OPENAI_API_KEY", "")
    """API key for the LLM provider (``OPENAI_API_KEY``)."""

    api_base: str = os.getenv("OPENAI_API_BASE", "")
    """Base URL for the LLM API (``OPENAI_API_BASE``)."""

    provider_name: str = os.getenv("PROVIDER_NAME", "openrouter")
    """Provider identifier used for OpenRouter header auto-detection."""

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------

    default_model: str = os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
    """Default model for chat requests (``LLM_MODEL_ID``)."""

    light_model: str = os.getenv(
        "LIGHT_MODEL_NAME", os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
    )
    """Cheap model for compression, classification, and other light tasks
    (``LIGHT_MODEL_NAME``).  Falls back to ``LLM_MODEL_ID``, then to
    ``"deepseek/deepseek-v4-flash"``."""

    # ------------------------------------------------------------------
    # Timeouts
    # ------------------------------------------------------------------

    timeout: int = int(os.getenv("LLM_TIMEOUT", "60"))
    """Request timeout in seconds (``LLM_TIMEOUT``)."""

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    project_root: Path = _PROJECT_ROOT
    """Absolute path to the project root directory."""

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    @classmethod
    def reload(cls) -> None:
        """Re-read the ``.env`` file and refresh all config values."""
        _load_dotenv()
        cls.api_key = os.getenv("OPENAI_API_KEY", "")
        cls.api_base = os.getenv("OPENAI_API_BASE", "")
        cls.provider_name = os.getenv("PROVIDER_NAME", "openrouter")
        cls.default_model = os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
        cls.light_model = os.getenv(
            "LIGHT_MODEL_NAME", os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
        )
        cls.timeout = int(os.getenv("LLM_TIMEOUT", "60"))
