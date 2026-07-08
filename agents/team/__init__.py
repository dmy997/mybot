"""Multi-agent team primitives — sub-agent runtime, blueprints, topologies.

This subpackage holds the *mechanics* of multi-agent coordination.  It is
intentionally NOT auto-discovered by :func:`agents.discover_agents` (which
only scans top-level ``agents/*.py`` modules).  Discovered paradigm agents
(e.g. ``agents/deep_research_agent.py``) compose these primitives.
"""

from __future__ import annotations

from .runner import SubAgentResult, SubAgentRunner, SubAgentSpec

__all__ = ["SubAgentRunner", "SubAgentSpec", "SubAgentResult"]
