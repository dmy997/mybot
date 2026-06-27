"""Textual ChatApp — the main CLI chat UI.

Replaces the prompt_toolkit loop + StreamRenderer with a full-screen
Textual TUI.  ``Orchestrator.process_message()`` is unchanged — only
the rendering layer is replaced.
"""

from __future__ import annotations

import time
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Header, Input, Static

from .widgets import (
    AssistantMessage,
    ErrorMessage,
    StatusFooter,
    StreamingMessage,
    ToolCallMessage,
    UserMessage,
)


class ChatApp(App):
    """Full-screen chat application powered by Textual.

    Widget tree::

        Header (#header-bar)
        VerticalScroll (#chat-area) ← messages mounted dynamically
        Horizontal (#input-area)
          Input (#user-input)
        StatusFooter (#status-bar)
    """

    CSS_PATH = "theme.css"
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        orchestrator: Any,  # Orchestrator (avoid circular import)
        session_key: str,
        model: str,
        is_resumed: bool = False,
    ) -> None:
        self._orche = orchestrator
        self._session_key = session_key
        self._model = model
        self._is_resumed = is_resumed
        self._t_start = 0.0
        super().__init__()

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="chat-area")
        with Horizontal(id="input-area"):
            yield Input(placeholder="Type a message...", id="user-input")
        yield StatusFooter(id="status-bar")

    async def on_mount(self) -> None:
        """Show welcome banner on first mount."""
        chat = self.query_one("#chat-area", VerticalScroll)
        mode = " (resumed)" if self._is_resumed else ""
        from rich.text import Text
        banner = Text(
            f"mybot — {self._model}{mode}\n"
            f"session: {self._session_key}\n"
            f"type /help for commands, /exit to quit",
            style="dim",
        )
        from rich.panel import Panel
        await chat.mount(Static(Panel(banner, border_style="dim blue", padding=(0, 1))))

    # -- input handling -------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # --- slash commands ---
        if text.lower() in ("/exit", "/quit"):
            self.exit()
            return

        if text.lower().startswith("/"):
            await self._handle_slash_command(text)
            event.input.clear()
            return

        event.input.clear()
        event.input.disabled = True

        self._t_start = time.monotonic()
        chat = self.query_one("#chat-area", VerticalScroll)

        # --- mount user message ---
        await chat.mount(UserMessage(text))
        chat.scroll_end(animate=False)

        # --- mount streaming placeholder ---
        stream = StreamingMessage()
        await chat.mount(stream)

        # tool widget tracking (name → widget)
        tool_widgets: dict[str, ToolCallMessage] = {}

        # --- callbacks (sync — Textual widgets are thread-safe within asyncio) ---

        async def _on_delta(token: str) -> None:
            stream.add_token(token)

        async def _on_thinking(token: str) -> None:
            pass  # spinner not needed — streaming widget is visible

        async def _on_thinking_done() -> None:
            pass

        async def _on_tool_start(name: str) -> None:
            w = ToolCallMessage(name, "running")
            await chat.mount(w)
            tool_widgets[name] = w

        async def _on_tool_end(ev: dict[str, Any]) -> None:
            name = ev.get("name", "")
            status = ev.get("status", "ok")
            if w := tool_widgets.get(name):
                w.set_status(status)

        async def _on_tool_exec_start(
            name: str, args: dict[str, Any], idx: int, total: int,
        ) -> None:
            pass  # already shown by _on_tool_start

        async def _on_tool_exec_end(ev: dict[str, Any]) -> None:
            pass  # _on_tool_end handles status

        async def _on_new_turn() -> None:
            # In Textual the streaming widget stays mounted across turns.
            # The VerticalScroll handles growing content correctly.
            pass

        # --- run agent ---
        try:
            result = await self._orche.process_message(
                session_key=self._session_key,
                user_input=text,
                on_delta=_on_delta,
                on_thinking=_on_thinking,
                on_thinking_done=_on_thinking_done,
                on_tool_start=_on_tool_start,
                on_tool_end=_on_tool_end,
                on_tool_execute_start=_on_tool_exec_start,
                on_tool_execute_end=_on_tool_exec_end,
                on_new_turn=_on_new_turn,
            )
        except Exception as exc:
            await chat.mount(ErrorMessage(str(exc)))
            event.input.disabled = False
            event.input.focus()
            return

        # --- finalize ---
        stream.finish()
        if not result.content and result.error:
            await chat.mount(ErrorMessage(result.error))

        elapsed = (time.monotonic() - self._t_start) * 1000
        usage = result.usage or {}
        self.query_one("#status-bar", StatusFooter).set_usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            elapsed_ms=elapsed,
            paradigm=result.paradigm or "",
        )

        chat.scroll_end(animate=False)
        event.input.disabled = False
        event.input.focus()

    async def _handle_slash_command(self, text: str) -> None:
        """Process /commands."""
        from rich.text import Text as RichText

        chat = self.query_one("#chat-area", VerticalScroll)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd == "/help":
            from .widgets import UserMessage
            msg = RichText(
                f"Session: {self._session_key}\nModel: {self._model}",
                style="dim",
            )
            await chat.mount(Static(msg))

        elif cmd == "/clear":
            for child in list(chat.children):
                await child.remove()
            self.query_one("#status-bar", StatusFooter).set_usage()

        elif cmd == "/history":
            session = self._orche.ctx.session.get_session(self._session_key)
            count = len(session.messages)
            msg = RichText(
                f"Session has {count} messages ({count // 2} exchanges).",
                style="dim",
            )
            await chat.mount(Static(msg))

        elif cmd == "/sessions":
            try:
                sessions = self._orche.ctx.session.list_sessions()
                names = [s.get("key", str(s)) for s in (sessions or [])]
                await chat.mount(Static(RichText(
                    "Sessions:\n" + "\n".join(f"  • {n}" for n in names) if names
                    else "No sessions found.",
                    style="dim",
                )))
            except Exception:
                await chat.mount(Static(RichText(
                    f"Session: {self._session_key}", style="dim"
                )))

    def action_quit(self) -> None:
        self.exit()
