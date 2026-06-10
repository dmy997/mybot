"""Rich-powered display helpers for the interactive REPL.

Encapsulates all ``rich`` usage so the orchestrator only calls simple
functions.  When stdout is not a TTY (piped, redirected, non-interactive),
falls back to plain-text output automatically.
"""

from __future__ import annotations

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


def show_tool_results(tool_events: list[dict[str, Any]]) -> None:
    """Render tool execution results as a table.

    Each event is ``{"name": str, "status": "ok"|"error", "detail": str,
    "duration_ms": float, "arguments": str}``.
    """
    if not tool_events:
        return

    table = Table(box=box.SIMPLE_HEAVY, border_style="dim blue", show_header=True)
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("tool", style="cyan", min_width=14)
    table.add_column("status", min_width=6, justify="center")
    table.add_column("duration", style="dim", width=8, justify="right")
    table.add_column("args", style="dim", max_width=40)
    table.add_column("detail", style="dim", max_width=60)

    for i, ev in enumerate(tool_events, 1):
        name = ev.get("name", "?")
        ok = ev.get("status") == "ok"
        status = "[green]✓[/green]" if ok else "[red]✗[/red]"
        dur = ev.get("duration_ms")
        dur_str = f"{dur:.0f}ms" if isinstance(dur, (int, float)) and dur > 0 else "-"
        args = (ev.get("arguments") or "")[:40].replace("\n", " ")
        detail = (ev.get("detail") or "")[:60].replace("\n", " ")
        table.add_row(str(i), name, status, dur_str, args, detail)

    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# LLM token / latency summary
# ---------------------------------------------------------------------------


def show_llm_usage(
    usage: dict[str, int],
    total_ms: float,
    steps: int,
) -> None:
    """Print a compact token-and-latency summary line."""
    tokens_total = usage.get("total_tokens", 0)
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    tokens_cache = usage.get("cache_read_input_tokens", 0)

    parts = [f"{tokens_total} tokens" if tokens_total else ""]
    if tokens_in or tokens_out:
        parts.append(f"in:{tokens_in} out:{tokens_out}")
    if tokens_cache:
        parts.append(f"cache:{tokens_cache}")
    parts.append(f"{total_ms:.0f}ms")
    if steps:
        parts.append(f"{steps} steps")

    line = "  ".join(p for p in parts if p)
    console.print(f"  [dim]── {line} ──[/dim]")


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
    console.file.flush() if console.file else None


def print_thinking_timer(elapsed: float) -> None:
    """Print or update the thinking indicator."""
    console.file.write(f"\r  ⏳ 思考中 ({elapsed:.1f}s)  ")
    console.file.flush()


def clear_thinking_timer() -> None:
    """Erase the thinking-timer line."""
    console.file.write("\r" + " " * 40 + "\r")
    console.file.flush()


# ---------------------------------------------------------------------------
# Tool call in-flight indicator
# ---------------------------------------------------------------------------


def print_tool_call_start(tool_name: str, arguments: dict[str, Any] | None = None) -> None:
    """Print a tool-call-in-progress indicator with key arguments."""
    args_str = _summarize_args_display(arguments)
    if args_str:
        console.print(
            f"  [dim cyan][tool:{tool_name}][/dim cyan] 执行中...  "
            f"[dim]{args_str}[/dim]",
            highlight=False,
        )
    else:
        console.print(
            f"  [dim cyan][tool:{tool_name}][/dim cyan] 执行中...",
            highlight=False,
        )


def print_tool_progress_start(
    name: str, args: dict[str, Any] | None, index: int, total: int,
) -> None:
    """Print an inline tool-execution start line."""
    args_str = _summarize_args_display(args, 50)
    console.print(
        f"  [dim cyan][{index}/{total}] {name}[/dim cyan] 执行中...  "
        f"[dim]{args_str}[/dim]",
        highlight=False,
    )


def print_tool_progress_end(ev: dict[str, Any]) -> None:
    """Print an inline tool-execution result line (overwrites start line)."""
    name = ev.get("name", "?")
    ok = ev.get("status") == "ok"
    status = "[green]✓[/green]" if ok else "[red]✗[/red]"
    dur = ev.get("duration_ms")
    dur_str = f"{dur:.0f}ms" if isinstance(dur, (int, float)) and dur > 0 else "-"
    args = (ev.get("arguments") or "")[:50]
    detail = (ev.get("detail") or "")[:60].replace("\n", " ")
    console.print(
        f"  {status} [cyan]{name}[/cyan]  [dim]{dur_str}[/dim]  "
        f"[dim]{args}[/dim]  [dim]{detail}[/dim]",
        highlight=False,
    )


def _summarize_args_display(args: dict[str, Any] | None, max_chars: int = 80) -> str:
    """Condense arguments for in-line display."""
    if not args:
        return ""
    text = str(args)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


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
