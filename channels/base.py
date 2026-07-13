"""Base classes and utilities shared across all chat channels.

Provides:
- ``ChannelMessage``: platform-agnostic message model
- ``BaseChannel``: ABC that every channel must implement
- ``build_orchestrator()``: shared factory for provider + orchestrator
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Normalized message model
# ---------------------------------------------------------------------------


@dataclass
class ChannelMessage:
    """Platform-agnostic message received from any chat channel.

    Each channel adapter is responsible for parsing its raw message dict
    into this normalized form before calling ``_process_message()``.
    """

    session_key: str
    """Unique session key for this (chat, user) pair."""

    text: str
    """Cleaned message text."""

    user_id: str
    """Platform-specific user identifier."""

    chat_id: str
    """Platform-specific chat identifier (same as user_id for private chats)."""

    chat_type: str
    """"private" or "group"."""

    platform: str
    """"wechat", "qq", "feishu", "telegram", etc."""

    raw: dict = field(default_factory=dict, repr=False)
    """Original platform message dict for channel-specific needs."""

    files: list[dict] = field(default_factory=list, repr=False)
    """Downloaded file attachments: ``[{"name": str, "path": str, "url": str}]``."""

    images: list[str] = field(default_factory=list, repr=False)
    """Base64 data URLs of images attached to this message."""


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BaseChannel(ABC):
    """Common interface for every chat channel adapter.

    Subclasses must implement:
    - ``start()`` — begin listening for messages
    - ``shutdown()`` — stop listening, clean up
    - ``send_reply()`` — send a text response back to the platform

    Subclasses may override:
    - ``_process_message()`` — custom pre/post-processing around the LLM call
    - ``_build_session_key()`` — custom session key scheme
    """

    channel_name: str = ""

    def __init__(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    # -- Subclass contract --------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Begin listening for incoming messages."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Stop listening and release resources."""

    @abstractmethod
    async def send_reply(self, text: str, msg: ChannelMessage) -> None:
        """Send a text reply back to the platform.

        Called after ``_process_message()`` produces a response.
        """

    async def send_file(
        self, file_path: str, filename: str, msg: ChannelMessage,
    ) -> None:
        """Send a file to the platform.  Override in channels that support it."""
        pass

    # -- Shared helpers ------------------------------------------------------

    @staticmethod
    def build_session_key(platform: str, chat_type: str, chat_id: str) -> str:
        """Construct a namespaced session key.

        Format: ``{platform}:{chat_type}:{chat_id}``
        """
        return f"{platform}:{chat_type}:{chat_id}"

    async def _process_message(self, msg: ChannelMessage) -> str | None:
        """Feed a normalized message to the LLM and return the reply text.

        Override if the channel needs pre/post-processing (e.g. rate limiting,
        command parsing, message dedup).
        """
        if not msg.text and not msg.images:
            return None

        collector: list[str] = []
        result = await self._orchestrator.process_message(
            session_key=msg.session_key,
            user_input=msg.text or "",
            images=msg.images or None,
            on_delta=lambda t: collector.append(t),
        )

        reply = (result.content or "").strip()
        return reply or None


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------


def build_orchestrator(*, log_config=None):
    """Create a provider + orchestrator from ``Config``.

    Shared by CLI, server, and all channel entry points so that
    every ``main()`` doesn't duplicate the same wiring.
    """
    from config import Config
    from core.orchestrator import Orchestrator
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key=Config.api_key,
        api_base=Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )

    orchestrator = Orchestrator(
        workspace=Path(Config.workspace).expanduser(),
        provider=provider,
        max_context_tokens=Config.context_window,
        max_output_tokens=Config.max_output_tokens,
        warning_buffer_ratio=Config.warning_buffer_ratio,
        auto_compact_buffer_ratio=Config.auto_compact_buffer_ratio,
        block_buffer_ratio=Config.block_buffer_ratio,
        compress_ratio=Config.compress_ratio,
        consolidation_ratio=Config.consolidation_ratio,
        idle_compress_seconds=Config.idle_compress_seconds,
        compress_model=Config.light_model,
        log_config=log_config,
    )

    return orchestrator
