"""Agent definitions — auto-discovery of paradigm agent classes."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any

from core.agent_base import BaseAgent


def discover_agents(provider: Any) -> dict[str, BaseAgent]:
    """Auto-discover all :class:`BaseAgent` subclasses in the agents package.

    Scans the ``agents/`` directory for Python modules, imports them,
    and instantiates every :class:`BaseAgent` subclass found. Each agent
    is keyed by its ``paradigm`` class attribute.

    Returns a dict suitable for passing to :class:`~core.dispatcher.Dispatcher`.
    """
    from core.runner import AgentCore

    agents: dict[str, BaseAgent] = {}
    agents_dir = Path(__file__).parent
    core = AgentCore(provider)

    for module_info in pkgutil.iter_modules([str(agents_dir)]):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"agents.{module_info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseAgent) and obj is not BaseAgent:
                instance = obj(core)
                agents[instance.paradigm] = instance

    return agents
