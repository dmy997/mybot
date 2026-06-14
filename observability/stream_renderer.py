"""Streaming renderer for CLI output.

Uses Rich for Markdown rendering with a throttled update strategy:
during streaming the buffer is accumulated and the Rich Live display is
updated at most every ~80ms (12 FPS).  This prevents the asyncio hot path
from being blocked by excessive Markdown object creation and Live update
overhead — the common streaming bottleneck in WSL2 terminals.

Flow per round:
  spinner → first delta → header + throttled Rich Live updates →
  on_end → stop Live (content stays on screen) + stop spinner
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager, nullcontext

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text


def _make_console() -> Console:
    """Create a Console that emits plain text when stdout is not a TTY."""
    return Console(file=sys.stdout, force_terminal=sys.stdout.isatty())


# ---------------------------------------------------------------------------
# ThinkingSpinner
# ---------------------------------------------------------------------------


class ThinkingSpinner:
    """Rich Status spinner showing "<name> is thinking..." with pause support."""

    def __init__(self, console: Console | None = None, name: str = "mybot"):
        c = console or _make_console()
        self._console = c
        self._spinner = c.status(f"[dim]{name} is thinking...[/dim]", spinner="dots")
        self._active = False

    def __enter__(self):
        self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        self._spinner.stop()
        return False

    @contextmanager
    def pause(self):
        """Temporarily stop the spinner for clean output."""
        if self._spinner and self._active:
            self._spinner.stop()
        try:
            yield
        finally:
            if self._spinner and self._active:
                self._spinner.start()

    @property
    def console(self) -> Console:
        return self._console


# ---------------------------------------------------------------------------
# StreamRenderer
# ---------------------------------------------------------------------------


class StreamRenderer:
    """Progressive Markdown renderer with throttled Rich Live updates.

    Tokens are accumulated into ``_buf`` as they arrive, but the Rich Live
    display is only updated every ``_UPDATE_INTERVAL`` seconds.  The refresh
    thread (``auto_refresh=True``) handles actual terminal output off the
    asyncio event loop, so the only asyncio cost per delta is a string
    concatenation.
    """

    _UPDATE_INTERVAL = 0.08  # throttle to ~12 FPS

    def __init__(
        self,
        render_markdown: bool = True,
        show_spinner: bool = True,
        bot_name: str = "mybot",
    ):
        self._md = render_markdown
        self._show_spinner = show_spinner
        self._bot_name = bot_name
        self._buf = ""
        self.streamed = False
        self._console = _make_console()
        self._live: Live | None = None
        self._spinner: ThinkingSpinner | None = None
        self._header_printed = False
        self._last_update = 0.0
        self._start_spinner()

    def _renderable(self):
        if self._md and self._buf:
            return Markdown(self._buf)
        return Text(self._buf or "")

    def _start_spinner(self) -> None:
        if self._show_spinner:
            self._spinner = ThinkingSpinner(console=self._console, name=self._bot_name)
            self._spinner.__enter__()

    def _stop_spinner(self) -> None:
        if self._spinner:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    def _ensure_header(self) -> None:
        """Stop spinner and print the assistant header once."""
        self._stop_spinner()
        if self._header_printed:
            return
        self._console.print()
        self._console.print(f"[cyan]{self._bot_name}[/cyan]")
        self._header_printed = True

    def _start_live(self) -> None:
        self._live = Live(
            self._renderable(),
            console=self._console,
            screen=False,
            auto_refresh=True,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()
        self._last_update = 0.0  # first delta after creation triggers update immediately

    # -- public properties / helpers ----------------------------------------

    @property
    def console(self) -> Console:
        return self._console

    @property
    def header_printed(self) -> bool:
        return self._header_printed

    def ensure_header(self) -> None:
        self._ensure_header()

    def pause(self):
        """Context manager: pause spinner for external output."""
        if self._spinner:
            return self._spinner.pause()
        return nullcontext()

    def pause_spinner(self):
        """Context manager: pause spinner for tool progress.

        Does **not** stop the Live display.  Rich's ``Live.console.print()``
        automatically suspends the live region while output is written
        through the same Console instance, so printing a tool-progress
        line via *self._console* is coordinated with the live display.
        """
        @contextmanager
        def _pause():
            with self._spinner.pause() if self._spinner else nullcontext():
                yield
        return _pause()

    def stop_for_input(self) -> None:
        """Stop spinner before user input to avoid prompt_toolkit conflicts."""
        self._stop_spinner()

    # -- streaming ----------------------------------------------------------

    async def on_delta(self, delta: str) -> None:
        """Accumulate delta and throttle Live updates."""
        self.streamed = True
        self._buf += delta
        if self._live is None:
            if not self._buf.strip():
                return
            self._ensure_header()
            self._start_live()
            return

        now = time.monotonic()
        if now - self._last_update >= self._UPDATE_INTERVAL:
            self._live.update(self._renderable())
            self._last_update = now

    async def on_end(self, *, resuming: bool = False) -> None:
        """Final update, stop Live, stop spinner."""
        if self._live:
            self._live.update(self._renderable())
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._stop_spinner()
        if resuming:
            self._buf = ""
            self._start_spinner()

    async def close(self) -> None:
        """Stop spinner/live without a final render."""
        if self._live:
            self._live.stop()
            self._live = None
        self._stop_spinner()
