"""Team blueprints — declarative multi-agent role configuration.

A blueprint says *what roles a team has, how each is prompted, and which
tools each may use* — pure data, no control flow.  A topology (e.g.
:class:`~agents.team.topology.OrchestratorWorkers`) reads a blueprint to
drive execution.

A new application (e.g. a code-review committee, a market-research team) is
a **new blueprint instance**, not new code — the mechanics stay fixed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerRole:
    """Configuration template applied to every spawned worker sub-agent."""

    system_prompt: str
    tool_names: tuple[str, ...] = ()
    """Parent tools the workers may use.  Empty tuple = all (minus ``delegate``)."""
    allow_network: bool = False
    allow_shell: bool = False
    model: str | None = None
    """Optional (usually cheaper) model for workers."""
    max_iterations: int = 8
    timeout_seconds: float = 180.0


@dataclass(frozen=True)
class TeamBlueprint:
    """Declarative spec for an orchestrator-workers team.

    Fields
    ------
    name:
        Stable identifier (also the report sub-directory / log tag).
    lead_prompt:
        System prompt instructing the lead to decompose a topic into a JSON
        array of independent subtasks.
    worker:
        Template for each spawned worker.
    synthesis_prompt:
        System prompt instructing the synthesizer to fuse worker findings
        into a full report plus a short executive summary.
    max_workers:
        Hard cap on the number of subtasks / workers spawned.
    max_concurrent:
        Concurrency cap for the parallel fan-out (bounds API cost / rate).
    lead_model / synthesis_model:
        Optional model overrides (e.g. a strong model for synthesis).
    """

    name: str
    lead_prompt: str
    worker: WorkerRole
    synthesis_prompt: str
    max_workers: int = 5
    max_concurrent: int = 3
    lead_model: str | None = None
    synthesis_model: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("TeamBlueprint.name must be non-empty")
        if self.max_workers < 1:
            raise ValueError("TeamBlueprint.max_workers must be >= 1")
        if self.max_concurrent < 1:
            raise ValueError("TeamBlueprint.max_concurrent must be >= 1")
