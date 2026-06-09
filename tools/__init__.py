"""Tool definitions for LLM function calling — auto-discovery."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from .registry import ToolRegistry
from .tool import Tool, ToolResult


def discover_tools(
    workspace: str | Path | None = None,
    *,
    timeout: int = 60,
) -> dict[str, Tool]:
    """Auto-discover all concrete :class:`Tool` subclasses in the tools package.

    Scans the ``tools/`` directory for Python modules, imports them, and
    instantiates every :class:`Tool` subclass whose ``name`` is set.
    Modules starting with ``_`` and the ``tool`` / ``registry`` modules are
    skipped.

    Constructor arguments (*workspace*, *timeout*) are forwarded to tool
    classes whose ``__init__`` accepts them by name; tools that don't accept
    a parameter receive the default.
    """
    tools: dict[str, Tool] = {}
    tools_dir = Path(__file__).parent
    _skip_modules = {"tool", "registry", "subagent", "memory_tools"}

    for module_info in pkgutil.iter_modules([str(tools_dir)]):
        name = module_info.name
        if name.startswith("_") or name in _skip_modules:
            continue

        module = importlib.import_module(f"tools.{name}")

        for _cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if not (issubclass(cls, Tool) and cls is not Tool):
                continue
            if not cls.name:
                continue  # abstract intermediate

            # Build kwargs dynamically based on what __init__ accepts
            kwargs = _build_init_kwargs(cls, workspace=workspace, timeout=timeout)
            instance = cls(**kwargs)
            tools[instance.name] = instance

    return tools


def _build_init_kwargs(
    cls: type[Tool],
    workspace: str | Path | None = None,
    timeout: int = 60,
) -> dict:
    """Return the subset of {workspace, timeout} accepted by *cls*.__init__."""
    kwargs: dict = {}
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return kwargs
    params = sig.parameters
    if "workspace" in params and workspace is not None:
        kwargs["workspace"] = workspace
    if "timeout" in params:
        kwargs["timeout"] = timeout
    return kwargs


__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "discover_tools",
]
