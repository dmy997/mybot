"""Textual widgets for the mybot chat UI."""

from __future__ import annotations

import time

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.containers import Vertical
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
# Session status — persistent status bar during turn execution
# ---------------------------------------------------------------------------


class SessionStatus(Static):
    """Persistent status bar with blinking dot — shows between chat and input.

    Visible only while a session turn is active.  Uses ``height: auto``
    + empty content to collapse to zero height when idle, so no layout
    shift occurs between turns.

    States (set via :meth:`set_status`):
    - ``"准备中..."`` — context building / compression
    - ``"思考中..."`` — LLM extended-thinking phase
    - ``"生成回复中..."`` — LLM streaming text
    - ``"工具执行中 (N)"`` — N tools currently executing
    """

    DEFAULT_CSS = """
    SessionStatus {
        height: auto;
        padding: 0 1;
    }
    """

    _BLINK_INTERVAL = 0.5

    def __init__(self, **kwargs) -> None:
        self._dot_on = True
        self._status = ""
        self._active = False
        super().__init__("", **kwargs)

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._BLINK_INTERVAL, self._blink)

    # -- public API ---------------------------------------------------------

    def show(self, status: str = "准备中...") -> None:
        """Make the bar visible and set initial status."""
        self._active = True
        self._status = status
        self._dot_on = True
        self._redraw()

    def set_status(self, status: str) -> None:
        """Update status text (e.g. ``"工具执行中 (3)"``)."""
        self._status = status
        self._redraw()

    def hide(self) -> None:
        """Collapse the bar — called when the turn finishes."""
        self._active = False
        self._status = ""
        self.update(Text(""))

    # -- internals ----------------------------------------------------------

    def _blink(self) -> None:
        if not self._active:
            return
        self._dot_on = not self._dot_on
        self._redraw()

    def _redraw(self) -> None:
        if not self._active or not self._status:
            self.update(Text(""))
            return
        dot = "●" if self._dot_on else "○"
        text = Text(f"  {dot} ", style="bold white")
        text.append(self._status, style="dim")
        self.update(text)


# ---------------------------------------------------------------------------
# Tool status — blinking dot during execution
# ---------------------------------------------------------------------------


class ToolStatus(_Bubble):
    """Tool execution indicator — pre-mounted, activated per tool call.

    - Inactive: ``display: none`` (zero height)
    - In progress: blinking ``●`` / ``○``
    - Success: static ``●`` (white)
    - Failure: static ``●`` (red) + error detail (max 100 chars) on a second line
    """

    DEFAULT_CSS = """
    ToolStatus {
        max-width: 88%;
        height: auto;
    }
    """

    _BLINK_INTERVAL = 0.4

    def __init__(self, **kwargs) -> None:
        self._tool_name = ""
        self._args_brief = ""
        self._dot_on = True
        self._done = True
        super().__init__("", **kwargs)
        # Always visible — empty content + height:auto = zero height when idle

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._BLINK_INTERVAL, self._blink)

    # -- public API ---------------------------------------------------------

    def activate(self, name: str, args_brief: str = "") -> None:
        """Bind this slot to a tool — show widget and start blinking."""
        self._tool_name = name
        self._args_brief = args_brief
        self._dot_on = True
        self._done = False
        self.update(self._build())

    def deactivate(self) -> None:
        """Reset slot to invisible idle state (empty content → height:auto → 0)."""
        self._done = True
        self._tool_name = ""
        self._args_brief = ""
        self.update(Text(""))

    async def mark_done(self) -> None:
        """Stop blinking, keep ● as static white dot."""
        self._done = True
        self._dot_on = True
        self.update(self._build())

    async def mark_error(self, detail: str = "") -> None:
        """Stop blinking, turn ● red with error detail below (max 100 chars)."""
        self._done = True
        text = Text("  ● ", style="bold red")
        text.append(self._tool_name, style="bold red")
        d = detail[:100].replace("\n", " ").strip()
        if d:
            text.append(f"\n     {d}", style="red")
        self.update(text)

    # -- internals ----------------------------------------------------------

    def _blink(self) -> None:
        if self._done:
            return
        self._dot_on = not self._dot_on
        self.update(self._build())

    def _build(self) -> Text:
        dot = "●" if self._dot_on else "○"
        text = Text(f"  {dot} ", style="bold white")
        text.append(self._tool_name, style="bold")
        if self._args_brief:
            text.append(f" ({self._args_brief})", style="dim")
        return text


class ToolDock(Vertical):
    """Pre-mounted container for :class:`ToolStatus` slots.

    Lives between the chat area and the input bar in the main layout.
    Children are pre-allocated so activating a slot is a pure
    ``update()`` — no DOM mount needed, so rendering is immediate
    even from ``@work`` context.
    """

    DEFAULT_CSS = """
    ToolDock {
        height: auto;
        max-height: 14;
        padding: 0 1;
        overflow-y: auto;
    }
    """


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
