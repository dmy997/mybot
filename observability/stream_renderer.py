"""Streaming renderer for CLI output.

Uses Rich Live with ``transient=True`` for in-place markdown updates during
streaming.  After the live display stops, a final clean render is printed
so the content persists on screen.
"""

from __future__ import annotations

import sys
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
    """Streaming renderer with Rich Live for in-place updates.

    During streaming: updates content in-place via Rich Live.
    On end: stops Live (transient=True erases it), then prints final render.

    Flow per round:
      spinner → first delta → header + Live updates →
      on_end → stop Live + final render
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
        self._live: Live | None = None
        self._spinner: ThinkingSpinner | None = None
        self._header_printed = False
        self._start_spinner()

    def _renderable(self):
        if self._md and self._buf:
            return Markdown(self._buf)
        return Text(self._buf or "")

    def _render_str(self) -> str:
        with self._console.capture() as cap:
            self._console.print(self._renderable())
        return cap.get()

    def _start_spinner(self) -> None:
        if self._show_spinner:
            self._spinner = ThinkingSpinner(console=self._console, name=self._bot_name)
            self._spinner.__enter__()

    def _stop_spinner(self) -> None:
        if self._spinner:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    @property
    def console(self) -> Console:
        return self._console

    @property
    def header_printed(self) -> bool:
        return self._header_printed

    def ensure_header(self) -> None:
        """Stop spinner and print the assistant header once."""
        self._stop_spinner()
        if self._header_printed:
            return
        self._console.print()
        self._console.print(f"[cyan]{self._bot_name}[/cyan]")
        self._header_printed = True

    def pause_spinner(self):
        """Context manager: temporarily stop transient output for clean trace lines."""
        @contextmanager
        def _pause():
            live_was_active = self._live is not None
            if self._live:
                self._live.stop()
                self._live = None
            with self._spinner.pause() if self._spinner else nullcontext():
                yield
            if live_was_active:
                return
        return _pause()

    async def on_delta(self, delta: str) -> None:
        self.streamed = True
        self._buf += delta
        if self._live is None:
            if not self._buf.strip():
                return
            self.ensure_header()
            self._live = Live(
                self._renderable(),
                console=self._console,
                auto_refresh=False,
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(self._renderable())
        self._live.refresh()

    async def on_end(self, *, resuming: bool = False) -> None:
        if self._live:
            self._live.refresh()
            self._live.update(self._renderable())
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._stop_spinner()
        if self._buf.strip():
            out = sys.stdout
            out.write(self._render_str())
            out.flush()
        if resuming:
            self._buf = ""
            self._start_spinner()

    def stop_for_input(self) -> None:
        """Stop spinner before user input to avoid prompt_toolkit conflicts."""
        self._stop_spinner()

    def pause(self):
        """Context manager: pause spinner for external output."""
        if self._spinner:
            return self._spinner.pause()
        return nullcontext()

    async def close(self) -> None:
        """Stop spinner/live without rendering a final streamed round."""
        if self._live:
            self._live.stop()
            self._live = None
        self._stop_spinner()
