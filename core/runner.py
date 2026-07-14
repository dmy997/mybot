"""Agent loop execution core — shared by all top-level agents."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from config import Config
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from context.context_manager import _estimate_message_tokens
from core.events import (
    AgentCompleted,
    AgentStallWarning,
    AgentStarted,
    AgentStepStarted,
    LLMResponseReady,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    bus,
)
from observability.trace import tracer
from providers.base import LLMProvider, LLMResponse
from providers.errors import FatalLLMError, RecoverableLLMError, RetryableLLMError
from tools import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from core.middleware import MiddlewareChain, MiddlewareContext

# ---------------------------------------------------------------------------
# Spec / result
# ---------------------------------------------------------------------------


@dataclass
class AgentInput:
    """Input spec for a single agent run."""

    init_messages: list[dict[str, Any]] = field(default_factory=list)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    goal: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    session_key: str = ""
    """Session identifier used for event publishing."""
    paradigm: str = ""
    """Agent paradigm name (react, plan_solve)."""
    on_content_delta: Callable[[str], Awaitable[None]] | None = None
    """Async callback invoked for each content token during streaming."""
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = None
    """Async callback invoked for each reasoning/thinking token during streaming."""
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    """Async callback invoked for each tool-call delta during streaming."""
    on_tool_execute_start: Callable[[str, dict[str, Any], int, int], Awaitable[None]] | None = None
    """Async callback invoked before each tool executes (name, args, idx, total)."""
    on_tool_execute_end: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    """Async callback invoked after each tool completes with the tool event dict."""
    on_new_turn: Callable[[], Awaitable[None]] | None = None
    """Async callback invoked at the start of each new LLM turn (after the first)."""
    checkpoint: bool = False
    """Enable checkpointing for this run.  Also controlled by ``MYBOT_CHECKPOINT`` env var."""
    reflect: bool = False
    """Enable a post-generation reflection pass that reviews and improves the output."""
    reflect_model: str | None = None
    """Model override for the reflection call (None = same as primary model)."""
    reflect_temperature: float | None = None
    """Temperature override for the reflection call (None = use class default)."""
    reflect_max_tokens: int | None = None
    """Max-tokens override for the reflection call."""


@dataclass
class AgentOutput:
    """Result from a single agent run."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    content: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    reflected: bool = False
    """Whether the output has been through a reflection pass."""
    prereflect_content: str = ""
    """Content before reflection (for comparison / debugging)."""


# ---------------------------------------------------------------------------
# Agent core
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ITERATIONS = 20
_DEFAULT_MAX_TOOL_RESULT_CHARS = 6_000
_STALL_WARNING_RATIO = 0.75  # fraction of max_iterations at which stall warning fires
_CHECKPOINT_VERSION = 1

_REFLECTION_PROMPT = (
    "请仔细检查你上面的回答，从以下角度逐一审查：\n"
    "1. **事实准确性** — 是否有事实错误或幻觉？引用的数据、日期、名称是否准确？\n"
    "2. **逻辑完整性** — 推理链条是否有漏洞？结论是否由前面的分析自然推导而来？\n"
    "3. **覆盖度** — 是否遗漏了用户问题中的要点？多角度/多实体是否都覆盖到了？\n"
    "4. **表述清晰度** — 是否简洁明了、无歧义、无冗余？\n"
    "\n"
    "如果发现问题，请给出**修正后的完整回答**（不是补充，是完整替换）。\n"
    "如果没有问题，请简要说明\"已核实无误\"后输出你原有的完整回答。"
)
_REFLECTION_TEMPERATURE = 0.3
_REFLECTION_MAX_TOKENS = 4096

# Lightweight compaction (fallback when CompactionService is not injected)
_LW_COMPACT_TRIGGER_RATIO = 0.8
_LW_COMPACT_KEEP_TURNS = 2
_LW_COMPACT_SUMMARY_CHARS = 200
_LW_COMPACT_MAX_RESULT_CHARS = 3000


def _summarize_args(args: dict[str, Any] | None, max_chars: int = 120) -> str:
    """Condense tool call arguments into a compact single-line preview."""
    if not args:
        return "{}"
    text = str(args)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


# ---------------------------------------------------------------------------
# Span I/O capture helpers — snapshot input/output for trace debugging
# ---------------------------------------------------------------------------

_MAX_INPUT_CHARS = 3_000
_MAX_OUTPUT_CHARS = 5_000
_MAX_THINKING_CHARS = 2_000


def _capture_llm_input(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Capture the last user message + model for the LLM span input."""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            last_user = content if isinstance(content, str) else str(content)
            break
    if len(last_user) > _MAX_INPUT_CHARS:
        last_user = last_user[:_MAX_INPUT_CHARS] + "..."
    return {
        "model": model,
        "messages_count": len(messages),
        "last_user_message": last_user,
    }


def _capture_llm_output(response: Any) -> dict[str, Any]:
    """Capture LLM response content, thinking, and tool calls."""
    output: dict[str, Any] = {}
    content = response.content or ""
    if content:
        output["content"] = (
            content[:_MAX_OUTPUT_CHARS] + "..."
            if len(content) > _MAX_OUTPUT_CHARS
            else content
        )
    thinking = getattr(response, "reasoning_content", "") or ""
    if thinking:
        output["thinking"] = (
            thinking[:_MAX_THINKING_CHARS] + "..."
            if len(thinking) > _MAX_THINKING_CHARS
            else thinking
        )
    if response.tool_calls:
        output["tool_calls"] = [
            {"name": tc.name, "args": _summarize_args(tc.arguments, 200)}
            for tc in response.tool_calls
        ]
    output["finish_reason"] = response.finish_reason
    return output


def _capture_tool_input(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Capture tool name and arguments for the tool span input."""
    args_str = str(arguments or {})
    if len(args_str) > _MAX_INPUT_CHARS:
        args_str = args_str[:_MAX_INPUT_CHARS] + "..."
    return {"tool": name, "arguments": args_str}


def _capture_tool_output(result: Any) -> dict[str, Any]:
    """Capture tool result for the tool span output."""
    content = result.content or ""
    if len(content) > _MAX_OUTPUT_CHARS:
        content = content[:_MAX_OUTPUT_CHARS] + "..."
    return {
        "success": result.success,
        "content": content,
        "error": result.error,
    }


def _dump_llm_messages(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
    step_count: int,
    model: str,
    tools_count: int,
) -> None:
    """Write the full LLM context window to disk when ``DUMP_LLM_MESSAGES`` is set.

    Output path: ``{workspace}/debug/{session_key}__step{step_count:03d}__{ts}.json``
    """
    if not Config.dump_llm_messages:
        return

    workspace = Config.workspace
    debug_dir = Path(workspace).expanduser() / "debug"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.opt(exception=True).warning("Cannot create debug dir {!s}", debug_dir)
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"{session_key or 'default'}__step{step_count:03d}__{ts}.json"
    filepath = debug_dir / filename

    payload = {
        "timestamp": ts,
        "session_key": session_key,
        "step": step_count,
        "model": model,
        "messages_count": len(messages),
        "tools_count": tools_count,
        "estimated_tokens": _estimate_message_tokens(messages),
        "messages": messages,
    }

    try:
        filepath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(
            "Dumped LLM messages to {!s} ({:,} tokens est.)",
            filepath, payload["estimated_tokens"],
        )
    except (OSError, TypeError, ValueError):
        logger.opt(exception=True).warning("Failed to dump LLM messages to {!s}", filepath)


class AgentCore:
    """Minimal agent execution loop shared by all top-level agents.

    Calls the LLM in a loop, executes requested tool calls, and feeds
    results back into the conversation until the model produces a final
    response or the iteration budget is exhausted.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        max_tool_result_chars: int = _DEFAULT_MAX_TOOL_RESULT_CHARS,
        middleware: MiddlewareChain | None = None,
        max_context_tokens: int = 200_000,
        compaction: Any | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        self.provider = provider
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.middleware = middleware
        self.max_context_tokens = max_context_tokens
        self.compaction = compaction  # optional CompactionService from context module
        self._workspace = Path(workspace).expanduser().resolve() if workspace else None
        self._last_compacted_turns: int = 0

    # -- public entry point ----------------------------------------------------

    async def run(self, spec: AgentInput) -> AgentOutput:
        """Execute the agent loop and return the final output."""
        # -- checkpoint: resume -------------------------------------------------
        _cp_enabled = self._checkpointing_enabled(spec)
        _checkpoint = self._load_checkpoint(spec) if _cp_enabled else None

        if _checkpoint is not None:
            messages = list(_checkpoint["messages"])
            step_count = int(_checkpoint["step_count"])
            tools_used = list(_checkpoint["tools_used"])
            tool_events = list(_checkpoint["tool_events"])
            total_usage = dict(_checkpoint["total_usage"])
        else:
            messages = list(spec.init_messages)
            if spec.goal:
                messages = self._inject_goal(messages, spec.goal)
            tools_used: list[str] = []
            tool_events: list[dict[str, str]] = []
            total_usage: dict[str, int] = {}
            step_count = 0

        tool_defs = spec.tools.get_definitions() if spec.tools else None

        # Middleware: agent start (skipped on resume)
        mw = self.middleware
        ctx = None
        if mw:
            from core.middleware import MiddlewareContext
            ctx = MiddlewareContext(messages=messages, step_count=step_count)
            if _checkpoint is None:
                await mw.run_agent_start(ctx)

        # Publish AgentStarted (skipped on resume)
        if _checkpoint is None:
            await bus.publish(AgentStarted(
                session_key=spec.session_key,
                paradigm=spec.paradigm,
                messages_count=len(messages),
                tools_count=len(tool_defs or []),
            ))

        try:
            for _ in range(self.max_iterations):
                step_count += 1

                # Notify new turn (for display reset)
                if step_count > 1 and spec.on_new_turn:
                    await spec.on_new_turn()

                await bus.publish(AgentStepStarted(
                    session_key=spec.session_key, step_count=step_count,
                ))

                # Stall detection: warn when step count passes a fraction of max_iterations
                _stall_threshold = max(10, int(self.max_iterations * _STALL_WARNING_RATIO))
                if step_count == _stall_threshold:
                    logger.warning(
                        "Agent reached {} steps ({}% of max {}) — possible stall or infinite loop",
                        step_count, int(_STALL_WARNING_RATIO * 100), self.max_iterations,
                    )
                    await bus.publish(AgentStallWarning(
                        session_key=spec.session_key, step_count=step_count,
                    ))

                # Middleware: agent step
                if mw:
                    ctx.step_count = step_count
                    ctx.messages = messages
                    async def _step_handler(_c: Any) -> bool:
                        return True

                    should_continue = await mw.run_agent_step(ctx, _step_handler)
                    if not should_continue:
                        output = AgentOutput(
                            messages=messages, tools_used=tools_used,
                            content="Agent stopped by middleware.",
                            usage=total_usage, stop_reason="middleware",
                            tool_events=tool_events,
                        )
                        if mw:
                            await mw.run_agent_end(ctx, output)
                        await bus.publish(AgentCompleted(
                            session_key=spec.session_key,
                            paradigm=spec.paradigm, steps=step_count,
                            stop_reason="middleware",
                        ))
                        return output

                # Compact context before LLM call (via CompactionService or lightweight fallback)
                compacted = self._compact_for_llm(messages, tool_defs)

                # Inject progress hint so the LLM can pace itself
                _hint = self._progress_hint(step_count, self.max_iterations)
                if _hint:
                    compacted = list(compacted) + [{"role": "system", "content": _hint}]

                # LLM call (wrapped by middleware when present)
                if mw:
                    ctx.messages = compacted
                    ctx.model = spec.model
                    ctx.temperature = spec.temperature
                    ctx.max_tokens = spec.max_tokens
                    ctx.tool_defs = tool_defs

                    async def _llm_handler(c: MiddlewareContext) -> LLMResponse:
                        resp = await self._call_llm(spec, c.messages, c.tool_defs, step_count=step_count)
                        c.llm_response = resp
                        return resp

                    response = await mw.run_llm_call(ctx, _llm_handler)
                else:
                    response = await self._call_llm(spec, compacted, tool_defs, step_count=step_count)

                # Accumulate token usage across all turns
                for k, v in response.usage.items():
                    total_usage[k] = total_usage.get(k, 0) + v

                logger.debug(
                    "Agent turn: finish={}, content_len={}, tool_calls={}",
                    response.finish_reason,
                    len(response.content or ""),
                    len(response.tool_calls),
                )

                # --- error path ---
                if response.finish_reason == "error":
                    output = AgentOutput(
                        messages=messages,
                        tools_used=tools_used,
                        content=response.content or "",
                        usage=total_usage,
                        stop_reason="error",
                        error=response.content or "LLM returned an error",
                        tool_events=tool_events,
                    )
                    if mw:
                        await mw.run_agent_end(ctx, output)
                    await bus.publish(AgentCompleted(
                        session_key=spec.session_key,
                        paradigm=spec.paradigm, steps=step_count,
                        total_latency_ms=0, stop_reason="error",
                        error=output.error,
                    ))
                    return output

                # --- tool-call path ---
                if response.tool_calls:
                    assistant_msg = self._build_assistant_tool_call_message(response)
                    messages.append(assistant_msg)

                    await self._execute_tool_calls(
                        response.tool_calls, spec, tools_used, tool_events, messages, mw, ctx,
                    )

                    # Checkpoint: save after each tool execution batch
                    if _cp_enabled:
                        self._save_checkpoint(
                            spec, messages, step_count,
                            tools_used, tool_events, total_usage,
                        )

                    continue  # feed tool results back to LLM

                # --- stop / final-content path ---
                final_content = response.content or response.reasoning_content or ""
                messages.append({"role": "assistant", "content": final_content})
                output = AgentOutput(
                    messages=messages,
                    tools_used=tools_used,
                    content=final_content,
                    usage=total_usage,
                    stop_reason=response.finish_reason,
                    tool_events=tool_events,
                )

                # --- optional reflection pass ---
                if spec.reflect and final_content:
                    reflected = await self._reflect(spec, messages, final_content)
                    if reflected:
                        output.prereflect_content = final_content
                        output.content = reflected
                        output.reflected = True

                if mw:
                    await mw.run_agent_end(ctx, output)
                await bus.publish(AgentCompleted(
                    session_key=spec.session_key,
                    paradigm=spec.paradigm, steps=step_count,
                    stop_reason=response.finish_reason,
                ))
                # Checkpoint: delete on successful completion
                if _cp_enabled:
                    self._delete_checkpoint(spec)
                return output

            # --- exhausted iteration budget ---
            # Best-effort: ask the LLM to summarise what it already gathered
            # so the user gets useful output instead of a dead-end error.
            if tools_used:
                try:
                    with tracer.span("agent.summarize_on_max_iterations"):
                        messages.append({
                            "role": "user",
                            "content": (
                                "你已达到最大执行轮次。请基于目前为止收集到的所有信息，"
                                "生成一份尽可能完整的最终回答。不要提及迭代次数或执行限制"
                                "——只需尽最大努力回答用户的原始请求。"
                            ),
                        })
                        summary_resp = await self._call_llm(
                            spec, messages, None,  # None → no tools
                            recovery_attempt=False, step_count=step_count,
                        )
                        if summary_resp.content:
                            messages.append({
                                "role": "assistant",
                                "content": summary_resp.content,
                            })
                            for k, v in (summary_resp.usage or {}).items():
                                total_usage[k] = total_usage.get(k, 0) + v
                            output = AgentOutput(
                                messages=messages,
                                tools_used=tools_used,
                                content=summary_resp.content,
                                usage=total_usage,
                                stop_reason="max_iterations",
                                tool_events=tool_events,
                            )
                            if mw:
                                await mw.run_agent_end(ctx, output)
                            await bus.publish(AgentCompleted(
                                session_key=spec.session_key,
                                paradigm=spec.paradigm, steps=step_count,
                                stop_reason="max_iterations",
                            ))
                            if _cp_enabled:
                                self._delete_checkpoint(spec)
                            return output
                except Exception:
                    logger.opt(exception=True).warning(
                        "Summarisation on max_iterations failed, falling back"
                    )

            await bus.publish(AgentStallWarning(
                session_key=spec.session_key, step_count=step_count,
            ))
            output = AgentOutput(
                messages=messages,
                tools_used=tools_used,
                content=(
                    "抱歉，Agent 已达到最大执行轮次限制。"
                    if tools_used
                    else "Agent stopped: maximum iterations reached."
                ),
                usage=total_usage,
                stop_reason="max_iterations",
                error=(
                    "Agent 执行轮次过多，建议简化问题或分解为更小的子任务"
                    if tools_used
                    else None
                ),
                tool_events=tool_events,
            )
            if mw:
                await mw.run_agent_end(ctx, output)
            await bus.publish(AgentCompleted(
                session_key=spec.session_key,
                paradigm=spec.paradigm, steps=step_count,
                stop_reason="max_iterations",
            ))
            # Checkpoint: delete on terminal state
            if _cp_enabled:
                self._delete_checkpoint(spec)
            return output

        except Exception:
            # Checkpoint preserved on exception — caller can resume
            if mw:
                await mw.run_agent_end(ctx, None)
            raise

    # -- checkpoint ------------------------------------------------------------

    @staticmethod
    def _checkpointing_enabled(spec: AgentInput) -> bool:
        """Return True when checkpointing is active for *spec*.

        Enabled when ``spec.checkpoint`` is True OR ``MYBOT_CHECKPOINT``
        is set to ``1``/``true``.  Disabled when session_key is empty.
        """
        if not spec.session_key:
            return False
        if spec.checkpoint:
            return True
        env = Config.mybot_checkpoint.strip().lower()
        return env in ("1", "true", "yes")

    def _checkpoint_path(self, spec: AgentInput) -> Path:
        """Return the checkpoint file path for *spec*."""
        if self._workspace:
            base = self._workspace
        else:
            base = Path(Config.workspace).expanduser().resolve()
        return base / "sessions" / f"{spec.session_key}_checkpoint.json"

    def _save_checkpoint(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        step_count: int,
        tools_used: list[str],
        tool_events: list[dict[str, Any]],
        total_usage: dict[str, int],
    ) -> None:
        """Atomically write a checkpoint file."""
        path = self._checkpoint_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": _CHECKPOINT_VERSION,
            "session_key": spec.session_key,
            "paradigm": spec.paradigm,
            "step_count": step_count,
            "messages": messages,
            "tools_used": tools_used,
            "tool_events": tool_events,
            "total_usage": total_usage,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            logger.opt(exception=True).warning("Failed to write checkpoint {!s}", path)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_checkpoint(self, spec: AgentInput) -> dict[str, Any] | None:
        """Load and validate a checkpoint file.

        Returns ``None`` (and deletes the corrupt file) when the file is
        missing, unparseable, or has mismatched version / missing fields.
        """
        path = self._checkpoint_path(spec)
        if not path.exists():
            return None

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Checkpoint {!s} unreadable ({}), discarding", path, exc)
            path.unlink(missing_ok=True)
            return None

        if data.get("version") != _CHECKPOINT_VERSION:
            logger.warning(
                "Checkpoint version {} != expected {}, discarding",
                data.get("version"), _CHECKPOINT_VERSION,
            )
            path.unlink(missing_ok=True)
            return None

        required = ["messages", "step_count", "tools_used", "tool_events", "total_usage"]
        missing = [f for f in required if f not in data]
        if missing:
            logger.warning("Checkpoint missing fields {}, discarding", missing)
            path.unlink(missing_ok=True)
            return None

        logger.info(
            "Resumed agent run from checkpoint: step={}, messages={}, session_key={!r}",
            data["step_count"], len(data["messages"]), spec.session_key,
        )
        return data

    def _delete_checkpoint(self, spec: AgentInput) -> None:
        """Remove the checkpoint file (idempotent)."""
        path = self._checkpoint_path(spec)
        try:
            if path.exists():
                path.unlink()
                logger.debug("Deleted checkpoint {!s}", path)
        except OSError:
            logger.opt(exception=True).warning("Failed to delete checkpoint {!s}", path)

    # -- progress hint ---------------------------------------------------------

    @staticmethod
    def _progress_hint(step: int, max_iterations: int) -> str | None:
        """Return a system hint so the LLM can pace itself across iterations.

        Early steps receive a lightweight counter; late steps receive urgency
        nudges that escalate as the budget shrinks.
        """
        remaining = max_iterations - step
        if remaining > 8:
            return None  # plenty of room — no hint needed
        if remaining > 5:
            return f"[执行进度] 第 {step}/{max_iterations} 步，剩余 {remaining} 轮。"
        if remaining > 3:
            return (
                f"[执行进度] 第 {step}/{max_iterations} 步，剩余 {remaining} 轮。"
                f" 请开始整合已获取的信息，优先给出结论而非继续展开新搜索。"
            )
        if remaining > 1:
            return (
                f"[执行进度 ⚠️] 只剩余 {remaining} 轮！"
                f" 请在下一轮或本轮给出最终回答，不要再发起新的工具调用，"
                f" 除非当前信息完全不足以回答用户问题。"
            )
        return (
            f"[执行进度 🚨 最后一轮] 本轮是最后一次执行机会。"
            f" 必须立即整合所有已收集信息，生成完整的最终回答。"
            f" 绝对不要发起任何工具调用。"
        )

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _inject_goal(
        messages: list[dict[str, Any]], goal: str
    ) -> list[dict[str, Any]]:
        """Append goal to the last user message so the prompt prefix stays cacheable.

        Dynamic content goes at the *end* of user
        messages, never prepended to the system prompt.  This keeps the system
        prompt + tool definitions prefix stable for prompt-cache hits across turns.
        """
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content", "")
                if isinstance(content, str):
                    messages[i] = {**messages[i], "content": f"{content}\n\n[Goal]\n{goal}"}
                else:
                    messages[i] = {
                        **messages[i],
                        "content": content + [{"type": "text", "text": f"[Goal]\n{goal}"}],
                    }
                return messages
        # No user message found — append a synthetic one (unusual path)
        messages.append({"role": "user", "content": f"[Goal]\n{goal}"})
        return messages

    # -- compaction (delegated to CompactionService or lightweight fallback) ----

    def _compact_for_llm(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Compact messages before an LLM call.

        When a :class:`CompactionService` is injected, delegates to its
        ``micro_compact`` (Layer 1 — rule-based, no LLM).  Otherwise falls
        back to a lightweight 3-step summary compaction.

        Skips compaction when no new tool turns were added since the last
        call — avoids redundant work on consecutive iterations.
        """
        # Skip if no new tool turns since last compaction
        current_turns = sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        if current_turns == self._last_compacted_turns:
            return list(messages)

        # Use injected service when available
        if self.compaction is not None:
            result = self.compaction.micro_compact(
                messages,
                keep_recent_turns=_LW_COMPACT_KEEP_TURNS,
            )
        else:
            result = self._lightweight_compact(messages)

        self._last_compacted_turns = current_turns
        return result

    @staticmethod
    def _lightweight_compact(
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 200_000,
        trigger_ratio: float = _LW_COMPACT_TRIGGER_RATIO,
        keep_turns: int = _LW_COMPACT_KEEP_TURNS,
        summary_chars: int = _LW_COMPACT_SUMMARY_CHARS,
        max_result_chars: int = _LW_COMPACT_MAX_RESULT_CHARS,
    ) -> list[dict[str, Any]]:
        """Lightweight 3-step compaction — fallback when no CompactionService.

        Returns a **new list** (original is never modified).

        1. Summarise tool results older than *keep_turns* turns
        2. Remove orphan tool results (no matching tool_call)
        3. Fill missing tool results (tool_call with no result)
        """
        threshold = int(max_tokens * trigger_ratio)
        if _estimate_message_tokens(messages) <= threshold:
            return list(messages)  # under budget: return a copy (contract)

        # Step 1: Summarise old tool results
        total_turns = sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        cutoff = max(0, total_turns - keep_turns)

        cleaned: list[dict[str, Any]] = []
        turn = 0
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                turn += 1

            if msg.get("role") == "tool" and turn <= cutoff:
                content = msg.get("content", "")
                if isinstance(content, str):
                    if not content.startswith("[Compacted]"):
                        summary = content[:summary_chars].replace("\n", " ")
                        suffix = "..." if len(content) > summary_chars else ""
                        cleaned.append({
                            **msg,
                            "content": f"[Compacted] {summary}{suffix}",
                        })
                    else:
                        cleaned.append(msg)
                else:
                    cleaned.append(msg)
            elif msg.get("role") == "tool":
                # Recent turn — hard-truncate if oversized
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > max_result_chars:
                    cleaned.append({
                        **msg,
                        "content": content[:max_result_chars] + "\n... (truncated)",
                    })
                else:
                    cleaned.append(msg)
            else:
                cleaned.append(msg)

        # Step 2: Remove orphan tool results
        valid_ids: set[str] = set()
        for msg in cleaned:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and "id" in tc:
                        valid_ids.add(tc["id"])
        cleaned = [
            m for m in cleaned
            if m.get("role") != "tool" or m.get("tool_call_id") in valid_ids
        ]

        # Step 3: Fill missing tool results
        result_ids: set[str] = set()
        for msg in cleaned:
            if msg.get("role") == "tool":
                result_ids.add(msg.get("tool_call_id", ""))

        final: list[dict[str, Any]] = []
        for msg in cleaned:
            final.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else ""
                    if tc_id and tc_id not in result_ids:
                        final.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[Tool result unavailable — compacted]",
                        })
                        result_ids.add(tc_id)

        logger.debug(
            "Lightweight compaction: {} messages → {} messages",
            len(messages), len(final),
        )
        return final

    async def _call_llm(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        *,
        recovery_attempt: bool = False,
        step_count: int = 0,
    ) -> LLMResponse:
        """Forward parameters to the provider, using retry-aware calls.

        On :class:`RecoverableLLMError`, applies mitigation (compress context,
        reduce max_tokens) and retries once.  :class:`FatalLLMError` and
        exhausted :class:`RetryableLLMError` are returned as error responses.
        """
        t_start = time.monotonic()
        model = spec.model or getattr(self.provider, "_default_model", None) or "unknown"

        # Debug: write full context window to disk when DUMP_LLM_MESSAGES is set
        _dump_llm_messages(
            messages,
            session_key=spec.session_key or "",
            step_count=step_count,
            model=model,
            tools_count=len(tool_defs or []),
        )

        with tracer.span("llm.chat", model=model, messages_count=len(messages),
                         tools_count=len(tool_defs or [])):
            try:
                if (spec.on_content_delta is not None
                        or spec.on_tool_call_delta is not None
                        or spec.on_thinking_delta is not None):
                    response = await self.provider.chat_stream_with_retry(
                        messages=messages,
                        tools=tool_defs or [],
                        model=spec.model,
                        max_tokens=spec.max_tokens,
                        temperature=spec.temperature,
                        on_content_delta=spec.on_content_delta,
                        on_thinking_delta=spec.on_thinking_delta,
                        on_tool_call_delta=spec.on_tool_call_delta,
                    )
                else:
                    response = await self.provider.chat_with_retry(
                        messages=messages,
                        tools=tool_defs or [],
                        model=spec.model,
                        max_tokens=spec.max_tokens,
                        temperature=spec.temperature,
                    )

                latency_ms = (time.monotonic() - t_start) * 1000
                usage = response.usage or {}
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)
                tokens_total = usage.get("total_tokens", tokens_in + tokens_out)

                # Attach token counts to current span for OTel export
                span = tracer.current_span()
                if span is not None:
                    span.attributes["tokens_in"] = tokens_in
                    span.attributes["tokens_out"] = tokens_out
                    span.attributes["tokens_total"] = tokens_total

                    # I/O capture for trace debugging
                    span.input = _capture_llm_input(messages, model)
                    span.output = _capture_llm_output(response)

                await bus.publish(LLMResponseReady(
                    session_key=spec.session_key,
                    step_count=0,  # caller should set this via ctx if needed
                    model=model,
                    latency_ms=latency_ms,
                    messages_count=len(messages),
                    tools_count=len(tool_defs or []),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    tokens_total=tokens_total,
                    finish_reason=response.finish_reason,
                ))

                return response

            except RecoverableLLMError as exc:
                await bus.publish(LLMResponseReady(
                    session_key=spec.session_key,
                    model=model,
                    latency_ms=(time.monotonic() - t_start) * 1000,
                    messages_count=len(messages),
                    tools_count=len(tool_defs or []),
                    finish_reason="error",
                    error=exc.info.message,
                ))
                # Apply recovery strategy (once only to avoid loops)
                if recovery_attempt:
                    logger.warning(
                        "Recovery already attempted for {!r}, giving up: {}",
                        exc.info.error_type, exc.info.message,
                    )
                    return self._error_response(exc.info)
                return await self._recover_and_retry(spec, messages, tool_defs, exc.info, step_count=step_count)

            except RetryableLLMError as exc:
                logger.error("LLM call failed after all retries: {}", exc.info.message)
                await bus.publish(LLMResponseReady(
                    session_key=spec.session_key,
                    model=model,
                    latency_ms=(time.monotonic() - t_start) * 1000,
                    messages_count=len(messages),
                    tools_count=len(tool_defs or []),
                    finish_reason="error",
                    error=exc.info.message,
                ))
                return self._error_response(exc.info)

            except FatalLLMError as exc:
                logger.error("Fatal LLM error: {}", exc.info.message)
                await bus.publish(LLMResponseReady(
                    session_key=spec.session_key,
                    model=model,
                    latency_ms=(time.monotonic() - t_start) * 1000,
                    messages_count=len(messages),
                    tools_count=len(tool_defs or []),
                    finish_reason="error",
                    error=exc.info.message,
                ))
                return self._error_response(exc.info)

            except Exception:
                await bus.publish(LLMResponseReady(
                    session_key=spec.session_key,
                    model=model,
                    latency_ms=(time.monotonic() - t_start) * 1000,
                    messages_count=len(messages),
                    tools_count=len(tool_defs or []),
                    finish_reason="error",
                    error="unexpected exception in LLM call",
                ))
                raise

    # -- recovery -------------------------------------------------------------

    async def _recover_and_retry(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,  # LLMErrorInfo
        *,
        step_count: int = 0,
    ) -> LLMResponse:
        """Attempt to mitigate a recoverable error and retry once."""
        error_type = info.error_type or "unknown"
        logger.info("Attempting recovery for error_type={!r}", error_type)

        if error_type == "context_length":
            return await self._recover_context_length(spec, messages, tool_defs, info, step_count=step_count)

        if error_type == "content_filter":
            return await self._recover_content_filter(spec, messages, tool_defs, info, step_count=step_count)

        # Unknown recoverable type — treat as fatal
        return self._error_response(info)

    async def _recover_context_length(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,
        *,
        step_count: int = 0,
    ) -> LLMResponse:
        """Compact context and retry; fall back to dropping if still too long."""
        # 1st attempt: compact more aggressively than normal
        if self.compaction is not None:
            compacted = self.compaction.micro_compact(messages, keep_recent_turns=1)
        else:
            reduced_budget = int(self.max_context_tokens * 0.6)
            compacted = self._lightweight_compact(messages, max_tokens=reduced_budget)
        if _estimate_message_tokens(compacted) < _estimate_message_tokens(messages):
            logger.warning(
                "Context-length recovery: {} tokens → {} tokens (compacted)",
                _estimate_message_tokens(messages), _estimate_message_tokens(compacted),
            )
            return await self._call_llm(spec, compacted, tool_defs, recovery_attempt=True, step_count=step_count)

        # 2nd attempt: drop oldest non-system messages
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        if len(other_msgs) <= 2:
            logger.warning("Context still too long after trimming to minimum")
            return self._error_response(info)

        keep = max(2, len(other_msgs) * 2 // 3)
        trimmed = system_msgs + other_msgs[-keep:]
        logger.warning(
            "Context-length recovery (fallback): {} messages → {} messages",
            len(messages), len(trimmed),
        )
        return await self._call_llm(spec, trimmed, tool_defs, recovery_attempt=True, step_count=step_count)

    async def _recover_content_filter(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,
        *,
        step_count: int = 0,
    ) -> LLMResponse:
        """Append a safety-compliance hint to the system prompt and retry."""
        hint = "Ensure all responses comply with safety and content policy guidelines."
        modified = list(messages)
        for i, msg in enumerate(modified):
            if msg.get("role") == "system":
                modified[i] = {
                    **msg,
                    "content": f"{msg['content']}\n\n{hint}",
                }
                break
        else:
            modified.insert(0, {"role": "system", "content": hint})

        logger.info("Content-filter recovery: appended compliance hint")
        return await self._call_llm(spec, modified, tool_defs, recovery_attempt=True, step_count=step_count)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _error_response(info: Any) -> LLMResponse:
        """Build an error :class:`LLMResponse` from an :class:`LLMErrorInfo`."""
        return LLMResponse(
            content=f"Error: {info.message}",
            finish_reason="error",
            error={"type": info.error_type, "message": info.message},
        )

    async def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        spec: AgentInput,
        tools_used: list[str],
        tool_events: list[dict[str, str]],
        messages: list[dict[str, Any]],
        mw: MiddlewareChain | None = None,
        ctx: Any = None,
    ) -> None:
        """Execute tool calls, running parallel-safe ones concurrently.

        Tool calls are split into two groups based on :attr:`Tool._parallel`:
        - Parallel-safe tools run concurrently via ``asyncio.gather``.
        - Serial-only tools run one at a time in the order received.

        Results are appended to *messages* in the original tool-call order.
        """
        tools = spec.tools
        total = len(tool_calls)

        # Split by parallel capability
        parallel_group: list[tuple[int, Any]] = []  # (index, tool_call)
        serial_calls: list[tuple[int, Any]] = []

        for idx, tc in enumerate(tool_calls):
            tool = tools.get(tc.name)
            if tool is not None and tool.parallel:
                parallel_group.append((idx, tc))
            else:
                serial_calls.append((idx, tc))

        async def _exec_one(tc: Any) -> tuple[ToolResult, float]:
            t0 = time.monotonic()

            if mw and ctx is not None:
                ctx.tool_name = tc.name
                ctx.tool_arguments = tc.arguments
                ctx.tools = tools

                async def _tool_handler(c: MiddlewareContext) -> ToolResult:
                    with tracer.span("tool.execute", tool_name=c.tool_name):
                        result = await tools.execute(c.tool_name, c.tool_arguments)
                        c.tool_result = result
                        _span = tracer.current_span()
                        if _span is not None:
                            _span.input = _capture_tool_input(c.tool_name, c.tool_arguments)
                            _span.output = _capture_tool_output(result)
                        return result

                result = await mw.run_tool_execute(ctx, _tool_handler)
                latency_ms = (time.monotonic() - t0) * 1000
                await bus.publish(ToolExecutionCompleted(
                    session_key=spec.session_key,
                    tool_name=tc.name,
                    success=result.success,
                    latency_ms=latency_ms,
                    error=result.error,
                ))
                return result, latency_ms

            with tracer.span("tool.execute", tool_name=tc.name):
                result = await tools.execute(tc.name, tc.arguments)
                _span = tracer.current_span()
                if _span is not None:
                    _span.input = _capture_tool_input(tc.name, tc.arguments)
                    _span.output = _capture_tool_output(result)
                latency_ms = (time.monotonic() - t0) * 1000
                await bus.publish(ToolExecutionCompleted(
                    session_key=spec.session_key,
                    tool_name=tc.name,
                    success=result.success,
                    latency_ms=latency_ms,
                    error=result.error,
                ))
                return result, latency_ms

        def _make_event(tc: Any, result: ToolResult, duration_ms: float) -> dict[str, Any]:
            args_preview = _summarize_args(tc.arguments, 120)
            return {
                "name": tc.name,
                "status": "ok" if result.success else "error",
                "detail": (result.content or result.error or "")[:400],
                "duration_ms": round(duration_ms, 1),
                "arguments": args_preview,
            }

        # Execute parallel group concurrently
        if parallel_group:
            # Fire start callbacks + publish events for all parallel tools
            for idx, tc in parallel_group:
                if spec.on_tool_execute_start:
                    await spec.on_tool_execute_start(
                        tc.name, tc.arguments or {}, idx + 1, total,
                    )
                await bus.publish(ToolExecutionStarted(
                    session_key=spec.session_key,
                    tool_name=tc.name,
                    arguments=tc.arguments or {},
                    index=idx + 1,
                    total=total,
                ))

            tasks = [_exec_one(tc) for _, tc in parallel_group]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (idx, tc), raw in zip(parallel_group, raw_results):
                if isinstance(raw, BaseException):
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Tool raised: {raw}",
                    )
                    duration_ms = 0.0
                    await bus.publish(ToolExecutionCompleted(
                        session_key=spec.session_key,
                        tool_name=tc.name,
                        success=False,
                        latency_ms=0,
                        error=str(raw),
                    ))
                else:
                    result, duration_ms = raw
                tools_used.append(tc.name)
                ev = _make_event(tc, result, duration_ms)
                tool_events.append(ev)
                messages.append(
                    self._build_tool_result_message(tc.id, result),
                )
                if spec.on_tool_execute_end:
                    await spec.on_tool_execute_end(ev)

        # Execute serial calls one at a time
        for idx, tc in serial_calls:
            if spec.on_tool_execute_start:
                await spec.on_tool_execute_start(
                    tc.name, tc.arguments or {}, idx + 1, total,
                )
            await bus.publish(ToolExecutionStarted(
                session_key=spec.session_key,
                tool_name=tc.name,
                arguments=tc.arguments or {},
                index=idx + 1,
                total=total,
            ))
            result, duration_ms = await _exec_one(tc)
            tools_used.append(tc.name)
            ev = _make_event(tc, result, duration_ms)
            tool_events.append(ev)
            messages.append(
                self._build_tool_result_message(tc.id, result),
            )
            if spec.on_tool_execute_end:
                await spec.on_tool_execute_end(ev)

    async def _reflect(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        content_before: str,
    ) -> str | None:
        """Run a reflection pass and return improved content, or None on failure."""
        from config import Config

        reflect_model = spec.reflect_model or Config.reflect_model or spec.model
        reflect_temp = (
            spec.reflect_temperature
            if spec.reflect_temperature is not None
            else Config.reflect_temperature
        )
        reflect_max = spec.reflect_max_tokens or Config.reflect_max_tokens

        reflect_prompt = Config.reflect_prompt
        messages.append({"role": "user", "content": reflect_prompt})

        try:
            with tracer.span("agent.reflect", model=reflect_model):
                response = await self.provider.chat_with_retry(
                    messages=[dict(m) for m in messages],
                    tools=[],
                    model=reflect_model,
                    max_tokens=reflect_max,
                    temperature=reflect_temp,
                )
        except Exception:
            logger.opt(exception=True).warning("Reflection call failed, returning original")
            return None

        if response.content:
            messages.append({"role": "assistant", "content": response.content})
            return response.content
        return None

    @staticmethod
    def _build_assistant_tool_call_message(
        response: LLMResponse,
    ) -> dict[str, Any]:
        """Build an assistant message carrying tool-call requests."""
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ],
        }

    def _build_tool_result_message(
        self,
        tool_call_id: str,
        result: Any,  # ToolResult
    ) -> dict[str, Any]:
        """Build a tool-result message, safely capping content length."""
        if result.success:
            content = result.content or ""
        else:
            content = f"Error: {result.error}" if result.error else "Tool returned an error"

        if len(content) > self.max_tool_result_chars:
            content = content[:self.max_tool_result_chars] + "\n... (truncated)"

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }


if __name__ == "__main__":
    from dotenv import load_dotenv

    from providers.openai_compatible_provider import OpenAICompatibleProvider
    load_dotenv()

    llm = OpenAICompatibleProvider(
        Config.api_key,
        Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )
    core = AgentCore(provider=llm)

    messages = [
        {"role": "system", "content": "你是一个个人AI助手"},
        {"role": "user", "content": "如何看待美国战争部披露UAP相关文件？"},
    ]
    mock_spec = AgentInput(
        init_messages = messages,
    )
    response = asyncio.run(core.run(mock_spec))
    print(f"助手回答:\n{response}\n")
