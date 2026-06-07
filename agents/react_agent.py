"""ReAct (Reasoning + Acting) agent.

The simplest paradigm: interleaved reasoning and tool calls within
a single AgentCore loop.  The "ReAct-ness" comes from the system
prompt instructing the model to think step by step between actions;
the execution loop itself is identical to a plain tool-calling agent.
"""

from __future__ import annotations

from core.agent_base import BaseAgent
from core.runner import AgentInput, AgentOutput


class ReActAgent(BaseAgent):
    """Agent that reasons and acts in a single interleaved loop."""

    paradigm = "react"

    async def run(self, spec: AgentInput) -> AgentOutput:
        """Delegate directly to AgentCore — one loop, no multi-pass."""
        return await self.core.run(spec)
