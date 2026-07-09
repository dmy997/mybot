"""BackgroundService — cron scheduler, scheduled tasks, and Dream pipeline.

Extracted from Orchestrator's MCPServicesMixin so background task
lifecycle is testable independently of the Orchestrator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from memory.dream import Dream
from services.cron import CronScheduler
from services.scheduled_tasks import ScheduledTaskService


class BackgroundService:
    """Owns :class:`CronScheduler`, :class:`ScheduledTaskService`, and :class:`Dream`.

    Parameters
    ----------
    workspace:
        Root directory (cron state is stored under ``workspace/cron/``).
    store:
        :class:`MemoryStore` for the Dream consolidation pipeline.
    provider:
        LLM provider for Dream summarisation.
    model:
        Model name override for Dream.
    on_run_agent:
        Callback for executing scheduled side-effect tasks through the
        agent pipeline.  Called as ``await on_run_agent(session_key, prompt, skills)``.
    """

    def __init__(
        self,
        workspace: Path,
        store: object,
        provider: object,
        model: str,
        *,
        on_run_agent: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._dream = Dream(store=store, provider=provider, model=model)
        self.cron = CronScheduler(
            state_dir=Path(workspace) / "cron",
            on_job=self._on_cron_job,
        )
        self.cron.register_job("dream", interval_hours=2)

        async def _noop(_sk: str, _p: str, _sl: list[str] | None) -> None:
            pass

        self._scheduled = ScheduledTaskService(
            Path(workspace),
            self.cron,
            run_agent=on_run_agent or _noop,
        )

    async def _on_cron_job(self, name: str) -> None:
        """Route cron job *name* to the appropriate handler."""
        if name == "dream":
            await self._dream.run()
        else:
            await self._scheduled.fire(name)

    @property
    def scheduled_tasks(self) -> ScheduledTaskService:
        """The unified scheduled-task service (chat-created + system tasks)."""
        return self._scheduled

    async def start(self) -> None:
        """Start the cron scheduler."""
        await self.cron.start()

    def stop(self) -> None:
        """Stop the cron scheduler."""
        self.cron.stop()
