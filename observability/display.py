"""Rich-powered display helpers for the interactive REPL.

Encapsulates all ``rich`` usage so the orchestrator only calls simple
functions.  When stdout is not a TTY (piped, redirected, non-interactive),
falls back to plain-text output automatically.
"""

from __future__ import annotations

import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console(highlight=False)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def show_banner(
    session_key: str,
    model: str,
    msg_count: int,
    agents: list[str],
) -> None:
    """Render the startup banner as a styled Panel."""
    lines = [
        f"[bold]session :[/bold] {session_key}",
        f"[bold]model   :[/bold] {model}",
        f"[bold]history :[/bold] {msg_count} 条消息",
        f"[bold]agents  :[/bold] {', '.join(agents)}",
        "",
        "  /help     /history     /clear     /sessions     /exit",
    ]
    panel = Panel(
        "\n".join(lines),
        title="mybot",
        border_style="green",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# Tool call results
# ---------------------------------------------------------------------------


def show_tool_results(tool_events: list[dict[str, str]]) -> None:
    """Render tool execution results as a table.

    Each event is ``{"name": str, "status": "ok"|"error", "detail": str}``.
    """
    if not tool_events:
        return

    table = Table(box=box.SIMPLE_HEAVY, border_style="dim blue", show_header=True)
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("tool", style="cyan", min_width=12)
    table.add_column("status", min_width=6, justify="center")
    table.add_column("detail", style="dim", max_width=80)

    for i, ev in enumerate(tool_events, 1):
        name = ev.get("name", "?")
        ok = ev.get("status") == "ok"
        status = "[green]✓[/green]" if ok else "[red]✗[/red]"
        detail = (ev.get("detail") or "")[:80].replace("\n", " ")
        table.add_row(str(i), name, status, detail)

    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# Markdown / content rendering
# ---------------------------------------------------------------------------


def render_content(content: str) -> None:
    """Render LLM output as Markdown with syntax-highlighted code blocks."""
    if not content.strip():
        return
    console.print(Markdown(content))


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def print_stream_delta(delta: str) -> None:
    """Print a single streaming token without a trailing newline."""
    console.print(delta, end="")


def print_thinking_timer(elapsed: float) -> None:
    """Print or update the thinking indicator (uses raw stdout for \\r overwrite)."""
    sys.stdout.write(f"\r  ⏳ 思考中 ({elapsed:.1f}s)  ")
    sys.stdout.flush()


def clear_thinking_timer() -> None:
    """Erase the thinking-timer line."""
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Tool call in-flight indicator
# ---------------------------------------------------------------------------


def print_tool_call_start(tool_name: str) -> None:
    """Print a tool-call-in-progress indicator."""
    console.print(f"  [dim cyan][tool:{tool_name}][/dim cyan] 执行中...", highlight=False)


# ---------------------------------------------------------------------------
# Plain-text fallback helpers
# ---------------------------------------------------------------------------


def print_plain(line: str = "", **kwargs: Any) -> None:
    """Print a line without rich markup (for error messages etc.)."""
    console.print(line, highlight=False, **kwargs)


def print_error(message: str) -> None:
    """Print an error message in red."""
    console.print(f"[red]Error:[/red] {message}", highlight=False)


# ---------------------------------------------------------------------------
# History & sessions
# ---------------------------------------------------------------------------


def show_history(session_key: str, messages: list[dict[str, Any]]) -> None:
    """Render conversation history as a table."""
    if not messages:
        console.print("  (暂无对话历史)")
        return

    table = Table(
        box=box.SIMPLE,
        border_style="dim blue",
        show_header=True,
        title=f"{session_key} 对话历史 ({len(messages)} 条消息)",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("role", style="cyan", min_width=10)
    table.add_column("preview", style="dim", max_width=80)

    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            preview = content[:120].replace("\n", " ")
            if len(content) > 120:
                preview += "..."
        else:
            preview = f"[{type(content).__name__}]"
        table.add_row(str(i), role, preview)

    console.print(table)
    console.print()


def show_sessions(sessions: list[dict[str, Any]]) -> None:
    """Render session list as a table."""
    if not sessions:
        console.print("  (暂无保存的会话)")
        return

    table = Table(
        box=box.SIMPLE,
        border_style="dim blue",
        show_header=True,
        title=f"所有会话 ({len(sessions)} 个)",
    )
    table.add_column("key", style="cyan", min_width=16)
    table.add_column("messages", justify="right", min_width=8)
    table.add_column("created", style="dim", min_width=20)

    for s in sessions:
        key = s.get("key", "?")
        msg_count = str(s.get("message_count", 0))
        created = str(s.get("created_at", "?"))
        table.add_row(key, msg_count, created)

    console.print(table)
    console.print()
