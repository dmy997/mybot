"""Abstract base class for paradigm-specific agents.

Each paradigm (ReAct, Plan-and-Solve, Reflexion, ReWOO, etc.) is a
subclass that implements its own orchestration strategy.  The base provides
shared utilities for message construction and AgentCore interaction.

Paradigm agents only describe *what* messages to send and in *what
sequence* to call AgentCore — they never touch LLM APIs or tool
execution directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .runner import AgentCore, AgentInput, AgentOutput


class BaseAgent(ABC):
    """Base class for agent paradigm implementations."""

    paradigm: str = "base"

    def __init__(self, core: AgentCore) -> None:
        self.core = core

    # -- abstract ----------------------------------------------------------

    @abstractmethod
    async def run(self, spec: AgentInput) -> AgentOutput:
        """Execute this paradigm's orchestration strategy.

        The simplest paradigms (e.g. ReAct) just delegate to
        ``self.core.run(spec)``.  Multi-pass paradigms (e.g. PlanSolve,
        Reflexion) orchestrate several AgentCore.run() calls with
        different configurations.

        The returned AgentOutput must include the full message history
        so the caller can continue the conversation regardless of which
        paradigm produced the last response.
        """
        ...

    # -- shared message builders -------------------------------------------

    @staticmethod
    def _user(content: str) -> dict[str, Any]:
        return {"role": "user", "content": content}

    # -- spec builders -----------------------------------------------------

    @staticmethod
    def _with_spec(spec: AgentInput, **overrides: Any) -> AgentInput:
        """Return a modified copy of *spec* with the given field overrides."""
        return AgentInput(
            init_messages=overrides.get("init_messages", spec.init_messages),
            tools=overrides.get("tools", spec.tools),
            goal=overrides.get("goal", spec.goal),
            model=overrides.get("model", spec.model),
            max_tokens=overrides.get("max_tokens", spec.max_tokens),
            temperature=overrides.get("temperature", spec.temperature),
            session_key=overrides.get("session_key", spec.session_key),
            paradigm=overrides.get("paradigm", spec.paradigm),
            on_content_delta=overrides.get("on_content_delta", spec.on_content_delta),
            on_thinking_delta=overrides.get("on_thinking_delta", spec.on_thinking_delta),
            on_tool_call_delta=overrides.get("on_tool_call_delta", spec.on_tool_call_delta),
            on_tool_execute_start=overrides.get("on_tool_execute_start", spec.on_tool_execute_start),
            on_tool_execute_end=overrides.get("on_tool_execute_end", spec.on_tool_execute_end),
            on_new_turn=overrides.get("on_new_turn", spec.on_new_turn),
            checkpoint=overrides.get("checkpoint", spec.checkpoint),
        )
