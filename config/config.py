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


# Inject settings.json "env" into os.environ (highest priority after shell env)
from config.settings import apply_settings_env

apply_settings_env()

# Load .env (lower priority than settings.json, skipped when key already set)
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

    multimodal_model: str = os.getenv(
        "MULTIMODAL_MODEL", "openai/gpt-4o-mini"
    )
    """Vision-capable model auto-switched to when images are attached but the
    current model does not support multimodal input (``MULTIMODAL_MODEL``)."""

    # ------------------------------------------------------------------
    # Timeouts
    # ------------------------------------------------------------------

    timeout: int = int(os.getenv("LLM_TIMEOUT", "60"))
    """Request timeout in seconds (``LLM_TIMEOUT``)."""

    # ------------------------------------------------------------------
    # Context window
    # ------------------------------------------------------------------

    context_window: int = int(os.getenv("CONTEXT_WINDOW", "200000"))
    """Main LLM context window in tokens (``CONTEXT_WINDOW``)."""

    max_output_tokens: int = int(os.getenv("MAX_OUTPUT_TOKENS", "20000"))
    """Tokens reserved for model output, subtracted from context window
    (``MAX_OUTPUT_TOKENS``)."""

    compress_ratio: float = float(os.getenv("COMPRESS_RATIO", "0.5"))
    """Fraction of context window reserved for recent messages during
    compression (``COMPRESS_RATIO``)."""

    consolidation_ratio: float = float(os.getenv("CONSOLIDATION_RATIO", "0.7"))
    """Fraction of context window that triggers background consolidation
    (``CONSOLIDATION_RATIO``)."""

    idle_compress_seconds: int = int(os.getenv("IDLE_COMPRESS_SECONDS", "300"))
    """Seconds of inactivity before idle compression kicks in
    (``IDLE_COMPRESS_SECONDS``, 0 = disabled)."""

    warning_buffer_ratio: float = float(os.getenv("WARNING_BUFFER_RATIO", "0.11"))
    """Fraction of effective_window reserved as warning buffer
    (``WARNING_BUFFER_RATIO``)."""

    auto_compact_buffer_ratio: float = float(
        os.getenv("AUTOCOMPACT_BUFFER_RATIO", "0.072")
    )
    """Fraction of effective_window reserved as auto-compact buffer
    (``AUTOCOMPACT_BUFFER_RATIO``)."""

    block_buffer_ratio: float = float(os.getenv("BLOCK_BUFFER_RATIO", "0.017"))
    """Fraction of effective_window reserved as block buffer
    (``BLOCK_BUFFER_RATIO``)."""

    max_session_messages: int = int(os.getenv("MAX_SESSION_MESSAGES", "2000"))
    """Hard cap on session message count — oldest messages are pruned first
    (``MAX_SESSION_MESSAGES``)."""

    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
    """Days of inactivity before a session is eligible for auto-deletion
    (``SESSION_TTL_DAYS``). Set to 0 to disable."""

    # ------------------------------------------------------------------
    # Hybrid search
    # ------------------------------------------------------------------

    hybrid_search_enabled: bool = (
        os.getenv("HYBRID_SEARCH_ENABLED", "true").lower() == "true"
    )
    """Enable hybrid search (SQLite + sqlite-vec + FTS5) for memory recall
    (``HYBRID_SEARCH_ENABLED``)."""

    embedding_model: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    """Sentence-transformers model for embedding memory chunks
    (``EMBEDDING_MODEL``)."""

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    mybot_api_key: str = os.getenv("MYBOT_API_KEY", "")
    """Bearer auth key for HTTP/WS endpoints (``MYBOT_API_KEY``).
    Auth is disabled when empty."""

    mybot_host: str = os.getenv("MYBOT_HOST", "127.0.0.1")
    """Server bind address (``MYBOT_HOST``)."""

    mybot_port: int = int(os.getenv("MYBOT_PORT", "8080"))
    """Server port (``MYBOT_PORT``)."""

    # ------------------------------------------------------------------
    # Checkpoint & debug
    # ------------------------------------------------------------------

    mybot_checkpoint: str = os.getenv("MYBOT_CHECKPOINT", "")
    """Enable long-task checkpoint recovery when non-empty
    (``MYBOT_CHECKPOINT``)."""

    dump_llm_messages: str = os.getenv("DUMP_LLM_MESSAGES", "")
    """Dump LLM messages to disk for debugging (``DUMP_LLM_MESSAGES``)."""

    # ------------------------------------------------------------------
    # External API keys
    # ------------------------------------------------------------------

    sandbox_backend: str = os.getenv("MYBOT_SANDBOX_BACKEND", "none")
    """Sandbox backend selection (``MYBOT_SANDBOX_BACKEND``)."""

    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    """Tavily search API key (``TAVILY_API_KEY``)."""

    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    """Google Custom Search API key (``GOOGLE_API_KEY``)."""

    google_cse_id: str = os.getenv("GOOGLE_CSE_ID", "")
    """Google Custom Search Engine ID (``GOOGLE_CSE_ID``)."""

    bing_api_key: str = os.getenv("BING_API_KEY", "")
    """Bing Search API key (``BING_API_KEY``)."""

    # ------------------------------------------------------------------
    # Channel-specific
    # ------------------------------------------------------------------

    xiaohongshu_fallback_chat: str = os.getenv(
        "XIAOHONGSHU_FALLBACK_CHAT", "filehelper"
    )
    """WeChat fallback contact for Xiaohongshu auto-publishing
    (``XIAOHONGSHU_FALLBACK_CHAT``)."""

    # ------------------------------------------------------------------
    # Human-in-the-loop
    # ------------------------------------------------------------------

    hitl_mode: str = os.getenv("HITL_MODE", "confirm")
    """HITL mode: ``"confirm"`` (require user approval for dangerous tools,
    default) or ``"bypass"`` (auto-execute all).  (``HITL_MODE``)."""

    hitl_bypass_tools: str = os.getenv("HITL_BYPASS_TOOLS", "xiaohongshu_publish")
    """Comma-separated tool names to bypass confirmation even in confirm mode
    (``HITL_BYPASS_TOOLS``)."""

    hitl_timeout_seconds: int = int(os.getenv("HITL_TIMEOUT_SECONDS", "120"))
    """Seconds to wait for user confirmation before auto-denying
    (``HITL_TIMEOUT_SECONDS``)."""

    hitl_server_url: str = os.getenv(
        "HITL_SERVER_URL",
        f"http://127.0.0.1:{os.getenv('MYBOT_PORT', '8080')}",
    )
    """Server URL for cross-process HITL bridge polling
    (``HITL_SERVER_URL``).  Defaults to ``http://127.0.0.1:8080``."""

    # ------------------------------------------------------------------
    # Reflection
    # ------------------------------------------------------------------

    reflect_enabled: bool = os.getenv("REFLECT_ENABLED", "false").lower() == "true"
    """Enable reflection by default for all agent runs (``REFLECT_ENABLED``).
    Can be overridden per-run via ``AgentInput.reflect``."""

    reflect_model: str = os.getenv("REFLECT_MODEL", "")
    """Model override for reflection calls (``REFLECT_MODEL``).
    Empty = same model as primary."""

    reflect_temperature: float = float(os.getenv("REFLECT_TEMPERATURE", "0.3"))
    """Temperature for reflection calls (``REFLECT_TEMPERATURE``)."""

    reflect_max_tokens: int = int(os.getenv("REFLECT_MAX_TOKENS", "4096"))
    """Max output tokens for a reflection call (``REFLECT_MAX_TOKENS``)."""

    reflect_prompt: str = os.getenv(
        "REFLECT_PROMPT",
        "请仔细检查你上面的回答，从以下角度逐一审查：\n"
        "1. 事实准确性 — 是否有事实错误或幻觉？引用的数据、日期、名称是否准确？\n"
        "2. 逻辑完整性 — 推理链条是否有漏洞？结论是否由分析自然推导而来？\n"
        "3. 覆盖度 — 是否遗漏了用户问题中的要点？\n"
        "4. 表述清晰度 — 是否简洁明了、无歧义、无冗余？\n"
        "\n"
        "如果发现问题，请给出修正后的完整回答（不是补充，是完整替换）。\n"
        "如果没有问题，请简要说明\"已核实无误\"后输出你原有的完整回答。",
    )
    """System prompt for the reflection pass (``REFLECT_PROMPT``)."""

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
        """Re-read ``settings.json`` and ``.env``, then refresh all config values."""
        from config.settings import reload_settings, apply_settings_env

        reload_settings()
        apply_settings_env()
        _load_dotenv()

        # Provider
        cls.api_key = os.getenv("OPENAI_API_KEY", "")
        cls.api_base = os.getenv("OPENAI_API_BASE", "")
        cls.provider_name = os.getenv("PROVIDER_NAME", "openrouter")

        # Model
        cls.default_model = os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
        cls.light_model = os.getenv(
            "LIGHT_MODEL_NAME", os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
        )
        cls.multimodal_model = os.getenv(
            "MULTIMODAL_MODEL", "openai/gpt-4o-mini"
        )

        # Timeouts & workspace
        cls.timeout = int(os.getenv("LLM_TIMEOUT", "60"))
        cls.workspace = os.getenv("WORKSPACE", "~/.mybot/workspace")

        # Context window
        cls.context_window = int(os.getenv("CONTEXT_WINDOW", "200000"))
        cls.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS", "20000"))
        cls.compress_ratio = float(os.getenv("COMPRESS_RATIO", "0.5"))
        cls.consolidation_ratio = float(os.getenv("CONSOLIDATION_RATIO", "0.7"))
        cls.idle_compress_seconds = int(os.getenv("IDLE_COMPRESS_SECONDS", "300"))
        cls.warning_buffer_ratio = float(os.getenv("WARNING_BUFFER_RATIO", "0.11"))
        cls.auto_compact_buffer_ratio = float(os.getenv("AUTOCOMPACT_BUFFER_RATIO", "0.072"))
        cls.block_buffer_ratio = float(os.getenv("BLOCK_BUFFER_RATIO", "0.017"))
        cls.max_session_messages = int(os.getenv("MAX_SESSION_MESSAGES", "2000"))
        cls.session_ttl_days = int(os.getenv("SESSION_TTL_DAYS", "30"))

        # Server
        cls.mybot_api_key = os.getenv("MYBOT_API_KEY", "")
        cls.mybot_host = os.getenv("MYBOT_HOST", "127.0.0.1")
        cls.mybot_port = int(os.getenv("MYBOT_PORT", "8080"))

        # Checkpoint & debug
        cls.mybot_checkpoint = os.getenv("MYBOT_CHECKPOINT", "")
        cls.dump_llm_messages = os.getenv("DUMP_LLM_MESSAGES", "")

        # External API keys
        cls.sandbox_backend = os.getenv("MYBOT_SANDBOX_BACKEND", "none")
        cls.tavily_api_key = os.getenv("TAVILY_API_KEY", "")
        cls.google_api_key = os.getenv("GOOGLE_API_KEY", "")
        cls.google_cse_id = os.getenv("GOOGLE_CSE_ID", "")
        cls.bing_api_key = os.getenv("BING_API_KEY", "")

        # Channel-specific
        cls.xiaohongshu_fallback_chat = os.getenv("XIAOHONGSHU_FALLBACK_CHAT", "filehelper")

        # HITL
        cls.hitl_mode = os.getenv("HITL_MODE", "confirm")
        cls.hitl_bypass_tools = os.getenv("HITL_BYPASS_TOOLS", "xiaohongshu_publish")
        cls.hitl_timeout_seconds = int(os.getenv("HITL_TIMEOUT_SECONDS", "120"))
        cls.hitl_server_url = os.getenv(
            "HITL_SERVER_URL",
            f"http://127.0.0.1:{os.getenv('MYBOT_PORT', '8080')}",
        )

        # Reflection
        cls.reflect_enabled = (
            os.getenv("REFLECT_ENABLED", "false").lower() == "true"
        )
        cls.reflect_model = os.getenv("REFLECT_MODEL", "")
        cls.reflect_temperature = float(
            os.getenv("REFLECT_TEMPERATURE", "0.3")
        )
        cls.reflect_max_tokens = int(
            os.getenv("REFLECT_MAX_TOKENS", "4096")
        )
        cls.reflect_prompt = os.getenv("REFLECT_PROMPT", cls.reflect_prompt)

        # Hybrid search
        cls.hybrid_search_enabled = (
            os.getenv("HYBRID_SEARCH_ENABLED", "true").lower() == "true"
        )
        cls.embedding_model = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
