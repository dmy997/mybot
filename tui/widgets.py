"""Textual widgets for the mybot chat UI."""

from __future__ import annotations

import time

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

# ---------------------------------------------------------------------------
# Message widgets
# ---------------------------------------------------------------------------


class _Bubble(Static):
    """Base for chat bubbles — width-constrained with auto-height."""

    DEFAULT_CSS = """
    _Bubble {
        width: auto;
        max-width: 88%;
        height: auto;
    }
    """

    def __init__(self, renderable: RenderableType, **kwargs) -> None:
        super().__init__(renderable, **kwargs)


class UserMessage(_Bubble):
    """Right-aligned user message — blue tinted bubble."""

    DEFAULT_CSS = """
    UserMessage {
        max-width: 72%;
    }
    """

    def __init__(self, content: str, **kwargs) -> None:
        text = Text(content)
        text.stylize("bold")
        panel = Panel(text, border_style="bright_blue", padding=(0, 1))
        super().__init__(panel, **kwargs)


class AssistantMessage(_Bubble):
    """Left-aligned assistant Markdown response."""

    def __init__(self, content: str, **kwargs) -> None:
        super().__init__(Markdown(content), **kwargs)


class StreamingMessage(_Bubble):
    """In-progress streaming response — live Markdown updates.

    Tokens are accumulated into a pending buffer and flushed to the
    reactive ``content`` attribute at most every ``_THROTTLE`` seconds
    so the layout system is not overwhelmed by per-token updates.
    """

    _THROTTLE = 0.08  # ~12 FPS

    content = reactive("")

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._pending = ""
        self._last_update = 0.0

    @property
    def raw_text(self) -> str:
        return self._pending

    @property
    def finished(self) -> bool:
        return False

    def add_token(self, token: str) -> None:
        """Accumulate a token and throttle-render via reactive."""
        self._pending += token
        now = time.monotonic()
        if now - self._last_update >= self._THROTTLE or len(self._pending) < 200:
            self.content = self._pending
            self._last_update = now

    def watch_content(self, content: str) -> None:
        """Render accumulated content as Markdown (called automatically by reactive)."""
        if content:
            self.update(Markdown(content))

    def _refresh(self) -> None:
        """Force immediate render — used for discrete events like tool calls."""
        self.content = self._pending
        self._last_update = time.monotonic()

    def finish(self) -> None:
        """Flush any remaining pending tokens and finalize."""
        self.content = self._pending

    def clear(self) -> None:
        """Reset buffer for a new turn."""
        self._pending = ""
        self.content = ""


# ---------------------------------------------------------------------------
# Tool status — blinking dot during execution
# ---------------------------------------------------------------------------


class ToolStatus(_Bubble):
    """Tool execution indicator with a blinking white dot.

    The dot blinks via :meth:`set_interval` while the tool runs.
    On success the widget is removed; on error it turns into a red
    error message.
    """

    DEFAULT_CSS = """
    ToolStatus {
        max-width: 88%;
    }
    """

    _BLINK_INTERVAL = 0.4

    def __init__(self, name: str, args_brief: str = "", **kwargs) -> None:
        self._tool_name = name
        self._args_brief = args_brief
        self._dot_on = True
        super().__init__(self._build(), **kwargs)

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._BLINK_INTERVAL, self._blink)

    def _blink(self) -> None:
        self._dot_on = not self._dot_on
        self.update(self._build())

    def _build(self) -> Text:
        dot = "●" if self._dot_on else "○"
        text = Text(f"  {dot} ", style="bold white")
        text.append(self._tool_name, style="bold")
        if self._args_brief:
            text.append(f" ({self._args_brief})", style="dim")
        return text

    def set_args(self, args_brief: str) -> None:
        """Update the argument display (called when full args are available)."""
        self._args_brief = args_brief
        self.update(self._build())

    async def mark_done(self) -> None:
        """Stop blinking and remove the widget (tool succeeded)."""
        if hasattr(self, "_timer"):
            self._timer.stop()
        await self.remove()

    async def mark_error(self, detail: str = "") -> None:
        """Stop blinking and show red error text (tool failed)."""
        if hasattr(self, "_timer"):
            self._timer.stop()
        text = Text("  ✗ ", style="bold red")
        text.append(self._tool_name, style="bold red")
        if detail:
            d = detail[:400].replace("\n", " ").strip()
            if d:
                text.append(f"  {d}", style="red")
        self.update(text)


# ---------------------------------------------------------------------------
# Footer / errors / layout
# ---------------------------------------------------------------------------


class StatusFooter(Static):
    """One-line footer showing token usage and latency."""

    DEFAULT_CSS = """
    StatusFooter {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._text = Text("", style="dim")

    def set_usage(
        self,
        session_key: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        elapsed_ms: float = 0,
        paradigm: str = "",
    ) -> None:
        parts = []
        if session_key:
            parts.append(f"Session: {session_key}")
        if prompt_tokens or completion_tokens:
            parts.append(
                f"Tokens: {prompt_tokens:,} in / {completion_tokens:,} out"
            )
        if elapsed_ms:
            parts.append(f"{elapsed_ms / 1000:.1f}s")
        if paradigm:
            parts.append(f"[{paradigm}]")
        self._text = Text("  ".join(parts), style="dim italic")
        self.update(self._text)


class ErrorMessage(_Bubble):
    """Red error message."""

    DEFAULT_CSS = """
    ErrorMessage {
        max-width: 88%;
    }
    """

    def __init__(self, error_text: str, **kwargs) -> None:
        text = Text(f"✗ {error_text}", style="bold red")
        super().__init__(text, **kwargs)


class ChatSpacer(Static):
    """Expanding spacer used in Horizontal rows for chat alignment."""

    DEFAULT_CSS = """
    ChatSpacer {
        width: 1fr;
        height: auto;
    }
    """
