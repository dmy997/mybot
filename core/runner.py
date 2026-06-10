"""Agent loop execution core — shared by all top-level agents."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from context.context_manager import _estimate_message_tokens
from observability.metrics import REGISTRY
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


# ---------------------------------------------------------------------------
# Agent core
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ITERATIONS = 20
_DEFAULT_MAX_TOOL_RESULT_CHARS = 16_000
_STALL_WARNING_STEPS = 50

# Compaction (lightweight context management during agent execution)
_RUNNER_MAX_CONTEXT_TOKENS = 128_000
_COMPACT_TRIGGER_RATIO = 0.8
_TOOL_SUMMARY_MAX_CHARS = 200
_RECENT_TOOL_TURNS = 2
_TOOL_RESULT_MAX_CHARS = 3000


def _summarize_args(args: dict[str, Any] | None, max_chars: int = 120) -> str:
    """Condense tool call arguments into a compact single-line preview."""
    if not args:
        return "{}"
    text = str(args)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


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
        max_context_tokens: int = _RUNNER_MAX_CONTEXT_TOKENS,
    ) -> None:
        self.provider = provider
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.middleware = middleware
        self.max_context_tokens = max_context_tokens

    # -- public entry point ----------------------------------------------------

    async def run(self, spec: AgentInput) -> AgentOutput:
        """Execute the agent loop and return the final output."""
        messages = list(spec.init_messages)
        if spec.goal:
            messages = self._inject_goal(messages, spec.goal)
        tools_used: list[str] = []
        tool_events: list[dict[str, str]] = []
        total_usage: dict[str, int] = {}
        step_count = 0

        tool_defs = spec.tools.get_definitions() if spec.tools else None

        # Middleware: agent start
        mw = self.middleware
        ctx = None
        if mw:
            from core.middleware import MiddlewareContext
            ctx = MiddlewareContext(messages=messages, step_count=step_count)
            await mw.run_agent_start(ctx)

        try:
            for _ in range(self.max_iterations):
                step_count += 1

                # Notify new turn (for display reset)
                if step_count > 1 and spec.on_new_turn:
                    await spec.on_new_turn()

                # Stall detection: warn when step count is abnormally high
                if step_count == _STALL_WARNING_STEPS:
                    logger.warning(
                        "Agent reached {} steps — possible stall or infinite loop",
                        step_count,
                    )
                    REGISTRY.agent_stall_warnings_total.inc()

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
                        return output

                # Compact context before LLM call (operates on a copy)
                compacted = self._maybe_compact(messages, tool_defs)

                # LLM call (wrapped by middleware when present)
                if mw:
                    ctx.messages = compacted
                    ctx.model = spec.model
                    ctx.temperature = spec.temperature
                    ctx.max_tokens = spec.max_tokens
                    ctx.tool_defs = tool_defs

                    async def _llm_handler(c: MiddlewareContext) -> LLMResponse:
                        resp = await self._call_llm(spec, c.messages, c.tool_defs)
                        c.llm_response = resp
                        return resp

                    response = await mw.run_llm_call(ctx, _llm_handler)
                else:
                    response = await self._call_llm(spec, compacted, tool_defs)

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
                    REGISTRY.agent_errors_total.inc()
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
                    return output

                # --- tool-call path ---
                if response.tool_calls:
                    assistant_msg = self._build_assistant_tool_call_message(response)
                    messages.append(assistant_msg)

                    await self._execute_tool_calls(
                        response.tool_calls, spec, tools_used, tool_events, messages, mw, ctx,
                    )

                    continue  # feed tool results back to LLM

                # --- stop / final-content path ---
                messages.append({"role": "assistant", "content": response.content or ""})
                REGISTRY.agent_steps.observe(step_count)
                output = AgentOutput(
                    messages=messages,
                    tools_used=tools_used,
                    content=response.content or "",
                    usage=total_usage,
                    stop_reason=response.finish_reason,
                    tool_events=tool_events,
                )
                if mw:
                    await mw.run_agent_end(ctx, output)
                return output

            # --- exhausted iteration budget ---
            REGISTRY.agent_steps.observe(step_count)
            REGISTRY.agent_stall_warnings_total.inc()
            output = AgentOutput(
                messages=messages,
                tools_used=tools_used,
                content="Agent stopped: maximum iterations reached.",
                usage=total_usage,
                stop_reason="max_iterations",
                tool_events=tool_events,
            )
            if mw:
                await mw.run_agent_end(ctx, output)
            return output

        except Exception:
            if mw:
                await mw.run_agent_end(ctx, None)
            raise

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _inject_goal(
        messages: list[dict[str, Any]], goal: str
    ) -> list[dict[str, Any]]:
        """Append goal to the last user message so the prompt prefix stays cacheable.

        Following nanobot's pattern: dynamic content goes at the *end* of user
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

    # -- compaction (lightweight context management on copies) -----------------

    @staticmethod
    def _remove_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Step 1: Remove tool results whose tool_call_id has no matching assistant tool_call."""
        valid_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and "id" in tc:
                        valid_ids.add(tc["id"])
        return [
            m for m in messages
            if m.get("role") != "tool" or m.get("tool_call_id") in valid_ids
        ]

    @staticmethod
    def _fill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Step 2: Insert placeholder for tool calls that have no result."""
        tool_result_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tool_result_ids.add(msg.get("tool_call_id", ""))

        result: list[dict[str, Any]] = []
        for msg in messages:
            result.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else ""
                    if tc_id and tc_id not in tool_result_ids:
                        result.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[Tool result unavailable — compacted]",
                        })
                        tool_result_ids.add(tc_id)
        return result

    @staticmethod
    def _summarize_old_tool_results(
        messages: list[dict[str, Any]],
        recent_turns: int = _RECENT_TOOL_TURNS,
    ) -> list[dict[str, Any]]:
        """Step 3: Compress old tool results to one-line summaries.

        Tool-calling turns are counted by assistant messages with tool_calls.
        Results from the last *recent_turns* are kept intact; older ones are
        replaced with ``[Compacted] {prefix}...``.
        """
        total_turns = sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        cutoff = max(0, total_turns - recent_turns)

        result: list[dict[str, Any]] = []
        turn = 0
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                turn += 1

            if msg.get("role") == "tool" and turn <= cutoff:
                content = msg.get("content", "")
                if isinstance(content, str) and not content.startswith("[Compacted]"):
                    summary = content[:_TOOL_SUMMARY_MAX_CHARS].replace("\n", " ")
                    suffix = "..." if len(content) > _TOOL_SUMMARY_MAX_CHARS else ""
                    result.append({
                        **msg,
                        "content": f"[Compacted] {summary}{suffix}",
                    })
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    @staticmethod
    def _truncate_long_tool_results(
        messages: list[dict[str, Any]],
        max_chars: int = _TOOL_RESULT_MAX_CHARS,
    ) -> list[dict[str, Any]]:
        """Step 4: Hard-truncate tool results that exceed *max_chars*.

        Only applies to non-current-turn results (last assistant with
        tool_calls marks the current turn).
        """
        # Find the last assistant-with-tool_calls index
        last_tool_call_idx = -1
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                last_tool_call_idx = i

        result: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and i < last_tool_call_idx:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > max_chars:
                    result.append({
                        **msg,
                        "content": content[:max_chars] + "\n... (truncated)",
                    })
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    @staticmethod
    def _truncate_by_token_budget(
        messages: list[dict[str, Any]],
        max_tokens: int,
        tool_defs: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Step 5: Drop oldest non-system messages until within token budget."""
        reserve = 4096  # output + tool definition overhead
        if tool_defs:
            reserve += _estimate_message_tokens(
                [{"role": "system", "content": str(tool_defs)}]
            )
        budget = max_tokens - reserve

        if _estimate_message_tokens(messages) <= budget:
            return list(messages)

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        while (
            _estimate_message_tokens(system_msgs + other_msgs) > budget
            and len(other_msgs) > 1
        ):
            other_msgs.pop(0)

        return system_msgs + other_msgs

    def _compact_context(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        """Run the full 7-step compaction pipeline on a copy.

        The original *messages* list is never modified.
        """
        cleaned = list(messages)

        # Step 1-2: Structural repair
        cleaned = self._remove_orphan_tool_results(cleaned)
        cleaned = self._fill_missing_tool_results(cleaned)

        # Step 3-4: Content reduction (old tool results)
        cleaned = self._summarize_old_tool_results(cleaned)
        cleaned = self._truncate_long_tool_results(cleaned)

        # Step 5: Token budget enforcement
        cleaned = self._truncate_by_token_budget(cleaned, max_tokens, tool_defs)

        # Step 6-7: Post-truncation repair
        cleaned = self._remove_orphan_tool_results(cleaned)
        cleaned = self._fill_missing_tool_results(cleaned)

        return cleaned

    def _maybe_compact(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Return compacted copy if token budget is tight, else original."""
        threshold = int(self.max_context_tokens * _COMPACT_TRIGGER_RATIO)
        if _estimate_message_tokens(messages) <= threshold:
            return messages
        logger.debug(
            "Compacting context: {} tokens exceeds threshold {}",
            _estimate_message_tokens(messages), threshold,
        )
        return self._compact_context(messages, tool_defs, self.max_context_tokens)

    async def _call_llm(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        *,
        recovery_attempt: bool = False,
    ) -> LLMResponse:
        """Forward parameters to the provider, using retry-aware calls.

        On :class:`RecoverableLLMError`, applies mitigation (compress context,
        reduce max_tokens) and retries once.  :class:`FatalLLMError` and
        exhausted :class:`RetryableLLMError` are returned as error responses.
        """
        t_start = time.monotonic()
        model = spec.model or getattr(self.provider, "default_model", "unknown")

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

                REGISTRY.llm_calls_total.inc()
                REGISTRY.llm_latency_ms.observe(latency_ms)
                if tokens_total:
                    REGISTRY.llm_tokens_total.inc(tokens_total)

                if response.finish_reason == "error":
                    REGISTRY.llm_calls_errors_total.inc()

                return response

            except RecoverableLLMError as exc:
                REGISTRY.llm_calls_total.inc()
                REGISTRY.llm_calls_errors_total.inc()
                # Apply recovery strategy (once only to avoid loops)
                if recovery_attempt:
                    logger.warning(
                        "Recovery already attempted for {!r}, giving up: {}",
                        exc.info.error_type, exc.info.message,
                    )
                    return self._error_response(exc.info)
                return await self._recover_and_retry(spec, messages, tool_defs, exc.info)

            except RetryableLLMError as exc:
                REGISTRY.llm_calls_total.inc()
                REGISTRY.llm_calls_errors_total.inc()
                logger.error("LLM call failed after all retries: {}", exc.info.message)
                return self._error_response(exc.info)

            except FatalLLMError as exc:
                REGISTRY.llm_calls_total.inc()
                REGISTRY.llm_calls_errors_total.inc()
                logger.error("Fatal LLM error: {}", exc.info.message)
                return self._error_response(exc.info)

            except Exception:
                REGISTRY.llm_calls_total.inc()
                REGISTRY.llm_calls_errors_total.inc()
                raise

    # -- recovery -------------------------------------------------------------

    async def _recover_and_retry(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,  # LLMErrorInfo
    ) -> LLMResponse:
        """Attempt to mitigate a recoverable error and retry once."""
        error_type = info.error_type or "unknown"
        logger.info("Attempting recovery for error_type={!r}", error_type)

        if error_type == "context_length":
            return await self._recover_context_length(spec, messages, tool_defs, info)

        if error_type == "content_filter":
            return await self._recover_content_filter(spec, messages, tool_defs, info)

        # Unknown recoverable type — treat as fatal
        return self._error_response(info)

    async def _recover_context_length(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,
    ) -> LLMResponse:
        """Compact context and retry; fall back to dropping if still too long."""
        # 1st attempt: compact with a tighter budget
        reduced_budget = int(self.max_context_tokens * 0.6)
        compacted = self._compact_context(messages, tool_defs, reduced_budget)
        if len(compacted) < len(messages):
            logger.warning(
                "Context-length recovery: {} messages → {} messages (compacted)",
                len(messages), len(compacted),
            )
            return await self._call_llm(spec, compacted, tool_defs, recovery_attempt=True)

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
        return await self._call_llm(spec, trimmed, tool_defs, recovery_attempt=True)

    async def _recover_content_filter(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
        info: Any,
    ) -> LLMResponse:
        """Append a safety-compliance hint to the system prompt and retry."""
        hint = "请确保所有回复内容安全合规，避免任何违反内容政策的表述。"
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
        return await self._call_llm(spec, modified, tool_defs, recovery_attempt=True)

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
                        latency_ms = (time.monotonic() - t0) * 1000
                        REGISTRY.tool_calls_total.inc()
                        REGISTRY.tool_latency_ms.observe(latency_ms)
                        if not result.success:
                            REGISTRY.tool_calls_errors_total.inc()
                        c.tool_result = result
                        return result

                result = await mw.run_tool_execute(ctx, _tool_handler)
                latency_ms = (time.monotonic() - t0) * 1000
                return result, latency_ms

            with tracer.span("tool.execute", tool_name=tc.name):
                result = await tools.execute(tc.name, tc.arguments)
                latency_ms = (time.monotonic() - t0) * 1000
                REGISTRY.tool_calls_total.inc()
                REGISTRY.tool_latency_ms.observe(latency_ms)
                if not result.success:
                    REGISTRY.tool_calls_errors_total.inc()
                return result, latency_ms

        def _make_event(tc: Any, result: ToolResult, duration_ms: float) -> dict[str, Any]:
            args_preview = _summarize_args(tc.arguments, 120)
            return {
                "name": tc.name,
                "status": "ok" if result.success else "error",
                "detail": (result.content or result.error or "")[:200],
                "duration_ms": round(duration_ms, 1),
                "arguments": args_preview,
            }

        # Execute parallel group concurrently
        if parallel_group:
            # Fire start callbacks for all parallel tools
            for idx, tc in parallel_group:
                if spec.on_tool_execute_start:
                    await spec.on_tool_execute_start(
                        tc.name, tc.arguments or {}, idx + 1, total,
                    )

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
                    REGISTRY.tool_calls_total.inc()
                    REGISTRY.tool_calls_errors_total.inc()
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
            result, duration_ms = await _exec_one(tc)
            tools_used.append(tc.name)
            ev = _make_event(tc, result, duration_ms)
            tool_events.append(ev)
            messages.append(
                self._build_tool_result_message(tc.id, result),
            )
            if spec.on_tool_execute_end:
                await spec.on_tool_execute_end(ev)

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
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENAI_API_BASE"),
        name=os.getenv("PROVIDER_NAME", "openrouter"),
        default_model=os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
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
