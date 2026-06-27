"""Streaming renderer for CLI output.

During streaming, tokens are written directly to the terminal so that
native terminal scrolling keeps the latest content visible.  Rich Live
is deliberately NOT used because its ``screen=False`` cursor-up escape
codes fail when the rendered content exceeds the terminal height,
causing new content to render below the visible viewport.

Flow per round:
  spinner -> first delta -> header + streamed tokens ->
  on_end -> reset buffer + stop spinner (or re-arm spinner for next turn)
"""

from __future__ import annotations

import sys
from contextlib import contextmanager, nullcontext

from rich.console import Console


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
    """Progressive text renderer for CLI streaming output.

    Tokens are written directly to the terminal as they arrive so that
    native terminal scrolling keeps the latest content visible regardless
    of buffer size.  No Rich Live is used — its cursor-up escape codes
    break when content exceeds terminal height.
    """

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
        self._spinner: ThinkingSpinner | None = None
        self._header_printed = False
        self._start_spinner()

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
        """Context manager: pause spinner for tool progress."""
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
        """Accumulate delta and write directly to terminal.

        No Rich Live is used — native terminal scrolling keeps the
        latest content visible regardless of buffer size.
        """
        self.streamed = True
        self._buf += delta
        if not self._header_printed:
            if not delta.strip():
                return
            self._ensure_header()
        self._console.file.write(delta)
        self._console.file.flush()

    async def on_end(self, *, resuming: bool = False) -> None:
        """Stop spinner, reset buffer for the next turn."""
        self._buf = ""
        self._stop_spinner()
        if resuming:
            self._start_spinner()

    async def close(self) -> None:
        """Stop spinner without final render (error/cleanup path)."""
        self._stop_spinner()
