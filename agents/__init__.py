"""Agent definitions — auto-discovery of paradigm agent classes."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agent_base import BaseAgent

if TYPE_CHECKING:
    from core.middleware import MiddlewareChain


def discover_agents(
    provider: Any,
    middleware: MiddlewareChain | None = None,
) -> dict[str, BaseAgent]:
    """Auto-discover all :class:`BaseAgent` subclasses in the agents package.

    Scans the ``agents/`` directory for Python modules, imports them,
    and instantiates every :class:`BaseAgent` subclass found. Each agent
    is keyed by its ``paradigm`` class attribute.

    Returns a dict suitable for passing to :class:`~core.dispatcher.Dispatcher`.
    """
    from config import Config
    from core.runner import AgentCore

    agents: dict[str, BaseAgent] = {}
    agents_dir = Path(__file__).parent
    core = AgentCore(
        provider,
        middleware=middleware,
        max_context_tokens=Config.context_window,
    )

    for module_info in pkgutil.iter_modules([str(agents_dir)]):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"agents.{module_info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseAgent) and obj is not BaseAgent:
                instance = obj(core)
                agents[instance.paradigm] = instance

    # Ensure "react" is always the first key so it becomes the default.
    # pkgutil returns modules in filesystem order (alphabetical), which
    # puts plan_solve before react — we reorder explicitly.
    if "react" in agents:
        react = agents.pop("react")
        agents = {"react": react, **agents}

    return agents
