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
    def _msg(role: str, content: str) -> dict[str, Any]:
        return {"role": role, "content": content}

    @staticmethod
    def _user(content: str) -> dict[str, Any]:
        return {"role": "user", "content": content}

    @staticmethod
    def _system(content: str) -> dict[str, Any]:
        return {"role": "system", "content": content}

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
        )
