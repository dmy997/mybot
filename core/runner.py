"""Agent loop execution core — shared by all top-level agents."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from providers.base import LLMProvider, LLMResponse
from tools import ToolRegistry

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
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    """Async callback invoked for each tool-call delta during streaming."""


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
    ) -> None:
        self.provider = provider
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars

    # -- public entry point ----------------------------------------------------

    async def run(self, spec: AgentInput) -> AgentOutput:
        """Execute the agent loop and return the final output."""
        messages = list(spec.init_messages)
        if spec.goal:
            messages = self._inject_goal(messages, spec.goal)
        tools_used: list[str] = []
        tool_events: list[dict[str, str]] = []
        total_usage: dict[str, int] = {}

        tool_defs = spec.tools.get_definitions() if spec.tools else None

        for _ in range(self.max_iterations):
            response = await self._call_llm(spec, messages, tool_defs)

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
                return AgentOutput(
                    messages=messages,
                    tools_used=tools_used,
                    content=response.content or "",
                    usage=total_usage,
                    stop_reason="error",
                    error=response.content or "LLM returned an error",
                    tool_events=tool_events,
                )

            # --- tool-call path ---
            if response.tool_calls:
                assistant_msg = self._build_assistant_tool_call_message(response)
                messages.append(assistant_msg)

                for tc in response.tool_calls:
                    tools_used.append(tc.name)
                    result = await spec.tools.execute(tc.name, tc.arguments)
                    tool_events.append({
                        "name": tc.name,
                        "status": "ok" if result.success else "error",
                        "detail": (result.content or result.error or "")[:200],
                    })
                    messages.append(
                        self._build_tool_result_message(tc.id, result),
                    )

                continue  # feed tool results back to LLM

            # --- stop / final-content path ---
            messages.append({"role": "assistant", "content": response.content or ""})
            return AgentOutput(
                messages=messages,
                tools_used=tools_used,
                content=response.content or "",
                usage=total_usage,
                stop_reason=response.finish_reason,
                tool_events=tool_events,
            )

        # --- exhausted iteration budget ---
        return AgentOutput(
            messages=messages,
            tools_used=tools_used,
            content="Agent stopped: maximum iterations reached.",
            usage=total_usage,
            stop_reason="max_iterations",
            tool_events=tool_events,
        )

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

    async def _call_llm(
        self,
        spec: AgentInput,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]] | None,
    ) -> LLMResponse:
        """Forward parameters to the provider, using streaming when callbacks are set."""
        if spec.on_content_delta is not None or spec.on_tool_call_delta is not None:
            return await self.provider.chat_stream(
                messages=messages,
                tools=tool_defs or [],
                model=spec.model,
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
                on_content_delta=spec.on_content_delta,
                on_tool_call_delta=spec.on_tool_call_delta,
            )
        return await self.provider.chat(
            messages=messages,
            tools=tool_defs or [],
            model=spec.model,
            max_tokens=spec.max_tokens,
            temperature=spec.temperature,
        )

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
