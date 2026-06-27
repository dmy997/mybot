"""Textual widgets for the mybot chat UI."""

from __future__ import annotations

import time

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static


# ---------------------------------------------------------------------------
# Message widgets
# ---------------------------------------------------------------------------


class UserMessage(Static):
    """A user message bubble — right-aligned, blue-tinted."""

    def __init__(self, content: str, **kwargs) -> None:
        text = Text(content)
        text.stylize("bold")
        panel = Panel(text, border_style="blue", padding=(0, 1))
        super().__init__(panel, **kwargs)


class AssistantMessage(Static):
    """A rendered assistant Markdown message."""

    def __init__(self, content: str, **kwargs) -> None:
        super().__init__(Markdown(content), **kwargs)


class StreamingMessage(Static):
    """In-progress streaming response — updates live with throttled rendering.

    Tokens are accumulated into an internal buffer.  The widget is
    re-rendered as Markdown at most every ``_THROTTLE`` seconds so
    that the Textual layout system is not overwhelmed by per-token
    updates on the hot path.
    """

    _THROTTLE = 0.08  # ~12 FPS

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._buf = ""
        self._last_update = 0.0
        self._complete = False

    @property
    def raw_text(self) -> str:
        return self._buf

    @property
    def finished(self) -> bool:
        return self._complete

    def add_token(self, token: str) -> None:
        """Accumulate a token and throttle-render."""
        self._buf += token
        now = time.monotonic()
        if now - self._last_update >= self._THROTTLE or len(self._buf) < 200:
            self._render()
            self._last_update = now

    def finish(self) -> None:
        """Final render — flush remaining content as Markdown."""
        self._complete = True
        self._render()

    def _render(self) -> None:
        if self._buf:
            self.update(Markdown(self._buf))

    def clear(self) -> None:
        """Reset buffer for a new turn."""
        self._buf = ""
        self._complete = False
        self.update("")


class ToolCallMessage(Static):
    """A compact tool-call indicator."""

    def __init__(self, name: str, status: str = "running", **kwargs) -> None:
        style = {
            "running": "dim cyan",
            "ok": "green",
            "error": "red",
        }.get(status, "dim")
        text = Text(f"  ⚙ {name}", style=style)
        super().__init__(text, **kwargs)

    def set_status(self, status: str, detail: str = "") -> None:
        style = {
            "running": "dim cyan",
            "ok": "green",
            "error": "red",
        }.get(status, "dim")
        text = Text(f"  ⚙ {status}  {detail}", style=style)
        self.update(text)


class StatusFooter(Static):
    """One-line footer showing token usage and latency."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._text = Text("", style="dim")

    def set_usage(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        elapsed_ms: float = 0,
        paradigm: str = "",
    ) -> None:
        parts = []
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


class ErrorMessage(Static):
    """Red error message."""

    def __init__(self, error_text: str, **kwargs) -> None:
        text = Text(f"  ✗ {error_text}", style="bold red")
        super().__init__(text, **kwargs)
