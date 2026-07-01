"""Textual ChatApp — the main CLI chat UI.

Replaces the prompt_toolkit loop + StreamRenderer with a full-screen
Textual TUI.  ``Orchestrator.process_message()`` is unchanged — only
the rendering layer is replaced.

Layout::

    Header (#header-bar)
    VerticalScroll (#chat-area)
      ├── <banner>
      ├── Horizontal (ChatSpacer + UserMessage)    ← user on right
      ├── Horizontal (AssistantMessage + ChatSpacer) ← agent on left
      │     ├── StreamingMessage (inline updates)
      │     └── ToolCallMessage (inline)
      └── ...
    SessionStatus (#session-status)  ← blinking dot + phase text
    Horizontal (#input-area)
      Input (#user-input)
    StatusFooter (#status-bar)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from .screens import ConfirmScreen, SessionListScreen
from .widgets import (
    AssistantMessage,
    ChatSpacer,
    ErrorMessage,
    SessionStatus,
    StatusFooter,
    StreamingMessage,
    UserMessage,
)


def _fmt_args(args: dict[str, Any], max_len: int = 60) -> str:
    """Format tool arguments into a compact one-line string."""
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        s = str(v)
        if len(s) > max_len:
            s = s[:max_len] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


class ChatApp(App):
    """Full-screen chat application powered by Textual.

    Messages are wrapped in ``Horizontal`` rows so that user bubbles sit on the
    right side and assistant responses sit on the left side.
    """

    CSS_PATH = "theme.css"
    BINDINGS = [
        ("ctrl+c", "quit_or_copy", "Quit / Copy"),
        Binding("escape", "cancel_message", "Cancel", show=False, priority=True),
        Binding("up", "history_prev", "", show=False, priority=True),
        Binding("down", "history_next", "", show=False, priority=True),
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
        self._goal: str | None = None
        self._input_history: list[str] = []
        self._history_index: int = -1
        self._saved_input: str = ""
        self._current_stream: StreamingMessage | None = None
        self._history_path: Path = (
            self._orche.workspace / "sessions" / f"{session_key}_input_history.json"
        )
        super().__init__()

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="chat-area")
        yield SessionStatus(id="session-status")
        with Horizontal(id="input-area"):
            yield Input(placeholder="Type a message...", id="user-input")
        yield StatusFooter(id="status-bar")
        yield Footer()

    def _load_history(self) -> None:
        """Load persisted input history from disk."""
        try:
            if self._history_path.exists():
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                if isinstance(data, list) and all(isinstance(v, str) for v in data):
                    self._input_history = data[-1000:]  # keep last 1000 entries
        except (json.JSONDecodeError, OSError):
            self._history_path.unlink(missing_ok=True)

    def _save_history(self) -> None:
        """Persist input history to disk (fire-and-forget)."""
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._history_path.write_text(
                json.dumps(self._input_history, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    async def on_mount(self) -> None:
        """Show welcome banner, disable mouse capture, focus input."""
        self._load_history()
        chat = self.query_one("#chat-area", VerticalScroll)
        mode = " (resumed)" if self._is_resumed else ""
        from rich.panel import Panel
        from rich.text import Text
        banner = Text(
            f"mybot — {self._model}{mode}\n"
            f"session: {self._session_key}\n"
            f"type /help for commands, /exit to quit",
            style="dim",
        )
        await chat.mount(Static(Panel(banner, border_style="dim blue", padding=(0, 1))))
        # Mouse scroll scrolls the chat area; hold Shift to select text natively.
        self.query_one("#user-input", Input).focus()

    # -- helpers --------------------------------------------------------------

    def _user_row(self, message: str) -> Horizontal:
        """Right-aligned user bubble: [spacer | bubble]."""
        return Horizontal(
            ChatSpacer(classes="chat-spacer"),
            UserMessage(message),
            classes="chat-row",
        )

    def _assistant_row(self, content: str = "") -> Horizontal:
        """Left-aligned assistant message: [content | spacer]."""
        return Horizontal(
            AssistantMessage(content),
            ChatSpacer(classes="chat-spacer"),
            classes="chat-row",
        )

    def _stream_row(self) -> tuple[Horizontal, StreamingMessage]:
        """Left-aligned streaming row: [stream | spacer]."""
        stream = StreamingMessage()
        row = Horizontal(
            stream,
            ChatSpacer(classes="chat-spacer"),
            classes="chat-row",
        )
        return row, stream

    def _error_row(self, error_text: str) -> Horizontal:
        """Left-aligned error row."""
        return Horizontal(
            ErrorMessage(error_text),
            ChatSpacer(classes="chat-spacer"),
            classes="chat-row",
        )

    # -- input handling -------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # --- slash commands ---
        if text.lower() in ("/exit", "/quit"):
            self._confirm_exit()
            event.input.clear()
            return

        if text.lower().startswith("/"):
            await self._handle_slash_command(text)
            event.input.clear()
            return

        # Save to input history (dedup consecutive identical entries)
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
            self._save_history()
        self._history_index = -1
        self._saved_input = ""

        event.input.clear()
        event.input.disabled = True
        self._t_start = time.monotonic()

        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.mount(self._user_row(text))
        chat.scroll_end(animate=False)

        stream_row, stream = self._stream_row()
        await chat.mount(stream_row)
        self._current_stream = stream

        # Activate session status bar
        self.query_one("#session-status", SessionStatus).show("准备中...")

        # Delegate to worker — non-blocking, exclusive=cancel previous
        self._run_chat(text, stream, chat)

    @work(exclusive=True, group="chat")
    async def _run_chat(
        self, text: str, stream: StreamingMessage, chat: VerticalScroll,
    ) -> None:
        """Background worker: runs process_message and updates UI via callbacks."""
        _last_scroll_time = 0.0
        _bar = self.query_one("#session-status", SessionStatus)
        _tool_count = 0
        _phase = "生成回复中..."

        def _render_bar() -> None:
            if _tool_count > 0:
                _bar.set_status(f"工具执行中 ({_tool_count})")
            else:
                _bar.set_status(_phase)

        async def _on_delta(token: str) -> None:
            nonlocal _last_scroll_time, _phase
            if _phase != "生成回复中...":
                _phase = "生成回复中..."
                _render_bar()
            stream.add_token(token)
            now = time.monotonic()
            if now - _last_scroll_time >= 0.15:
                chat.scroll_end(animate=False)
                _last_scroll_time = now

        async def _on_thinking(token: str) -> None:
            nonlocal _phase
            if _phase != "思考中...":
                _phase = "思考中..."
                _render_bar()

        async def _on_thinking_done() -> None:
            nonlocal _phase
            _phase = "生成回复中..."
            _render_bar()

        async def _on_tool_start(name: str, args_brief: str = "") -> None:
            nonlocal _phase
            _phase = "工具调用中..."
            _render_bar()

        async def _on_tool_end(ev: dict[str, Any]) -> None:
            pass

        async def _on_tool_exec_start(
            name: str, args: dict[str, Any], idx: int, total: int,
        ) -> None:
            nonlocal _tool_count
            _tool_count += 1
            _render_bar()
            # Tool call indicator in chat — guaranteed visible via stream text
            brief = _fmt_args(args)
            label = f" ({idx}/{total})" if total > 1 else ""
            line = f"\n\n  ⚙ **{name}**{label}"
            if brief:
                line += f"\n  _{brief}_"
            line += "\n"
            stream.add_token(line)
            stream._refresh()

        async def _on_tool_exec_end(ev: dict[str, Any]) -> None:
            nonlocal _tool_count
            if _tool_count > 0:
                _tool_count -= 1
            _render_bar()
            status = ev.get("status", "ok")
            if status == "error":
                detail = (ev.get("detail", "") or "")[:100].replace("\n", " ").strip()
                if detail:
                    stream.add_token(f"  ❌ {detail}\n")
                    stream._refresh()

        async def _on_new_turn() -> None:
            nonlocal _phase
            stream.add_token("\n\n")
            _phase = "生成回复中..."
            _render_bar()

        try:
            result = await self._orche.process_message(
                session_key=self._session_key,
                user_input=text,
                goal=self._goal,
                on_delta=_on_delta,
                on_thinking=_on_thinking,
                on_thinking_done=_on_thinking_done,
                on_tool_start=_on_tool_start,
                on_tool_end=_on_tool_end,
                on_tool_execute_start=_on_tool_exec_start,
                on_tool_execute_end=_on_tool_exec_end,
                on_new_turn=_on_new_turn,
            )
        except asyncio.CancelledError:
            stream.add_token("\n\n*[cancelled]*\n")
            return
        except Exception as exc:
            await chat.mount(self._error_row(str(exc)))
            return
        finally:
            _bar.hide()
            input_w = self.query_one("#user-input", Input)
            input_w.disabled = False
            input_w.focus()

        # --- finalize ---
        stream.finish()
        if not result.content and result.error:
            await chat.mount(self._error_row(result.error))

        elapsed = (time.monotonic() - self._t_start) * 1000
        usage = result.usage or {}
        self.query_one("#status-bar", StatusFooter).set_usage(
            session_key=self._session_key,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            elapsed_ms=elapsed,
            paradigm=result.paradigm or "",
        )
        chat.scroll_end(animate=False)

    def _confirm_exit(self) -> None:
        """Show confirmation dialog before quitting."""
        self.push_screen(
            ConfirmScreen("Are you sure you want to quit?"),
            lambda result: self.exit() if result else None,
        )

    def _confirm_clear(self) -> None:
        """Show confirmation dialog before clearing the chat."""
        def _on_result(result: bool) -> None:
            if result:
                chat = self.query_one("#chat-area", VerticalScroll)
                for child in list(chat.children):
                    child.remove()

        self.push_screen(
            ConfirmScreen("Are you sure you want to clear the chat?"),
            _on_result,
        )

    async def _handle_slash_command(self, text: str) -> None:
        """Process /commands."""
        from rich.text import Text as RichText

        chat = self.query_one("#chat-area", VerticalScroll)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd == "/goal":
            goal_text = parts[1].strip() if len(parts) > 1 else ""
            if goal_text:
                self._goal = goal_text
                msg = RichText(f"Goal set: {goal_text}", style="bold green")
            else:
                self._goal = None
                msg = RichText("Goal cleared.", style="dim")
            await chat.mount(Static(msg))

        elif cmd == "/help":
            msg = RichText(
                f"Session: {self._session_key}\nModel: {self._model}\n"
                f"Goal: {self._goal or '(none)'}",
                style="dim",
            )
            await chat.mount(Static(msg))

        elif cmd == "/clear":
            self._confirm_clear()

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
                self.push_screen(
                    SessionListScreen(sessions or []),
                    lambda _: None,
                )
            except Exception:
                await chat.mount(Static(RichText(
                    f"Session: {self._session_key}", style="dim"
                )))

    def action_history_prev(self) -> None:
        """Navigate to the previous entry in input history."""
        inp = self.query_one("#user-input", Input)
        if not inp.has_focus or not self._input_history:
            return

        if self._history_index == -1:
            self._saved_input = inp.value
            self._history_index = len(self._input_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return

        inp.value = self._input_history[self._history_index]
        inp.cursor_position = len(inp.value)

    def action_history_next(self) -> None:
        """Navigate to the next entry in input history."""
        inp = self.query_one("#user-input", Input)
        if not inp.has_focus or self._history_index == -1:
            return

        if self._history_index < len(self._input_history) - 1:
            self._history_index += 1
            inp.value = self._input_history[self._history_index]
        else:
            self._history_index = -1
            inp.value = self._saved_input

        inp.cursor_position = len(inp.value)

    def action_cancel_message(self) -> None:
        """Cancel the current message processing (escape)."""
        inp = self.query_one("#user-input", Input)
        if inp.has_focus:
            # Input has focus — clear it and let Textual handle escape internally
            return
        for w in self.workers:
            if w.group == "chat" and not w.is_finished:
                w.cancel()
                break

    def action_quit(self) -> None:
        self.exit()

    def action_quit_or_copy(self) -> None:
        """Copy selected text to clipboard; quit if nothing is selected."""
        selection = self.screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
        else:
            self.exit()
