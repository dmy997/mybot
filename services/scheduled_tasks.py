"""ScheduledTaskService — chat-created periodic tasks on top of CronScheduler.

A scheduled task is "at cron time T, run the agent with prompt P in session K".
Two flavours share one model, differing only in the *delivery* step:

- **push task** (``channel`` set): the fired prompt is injected into the
  MessageBus so the normal ``serve() → outbound(channel) → consumer`` pipeline
  pushes the result back to the user's chat.  Delivery is an injected
  ``deliver`` callback so this service never imports any channel.

- **internal / side-effect task** (``channel is None``): the fired prompt runs
  via an injected ``run_agent`` callback (``orchestrator.process_message``) and
  the output is discarded — the agent produces its effect through tools
  (e.g. the Xiaohongshu publish flow).

User tasks are persisted to ``{workspace}/scheduled_tasks.json`` and re-loaded
on startup.  System tasks (``system=True``) are re-seeded from code every launch
and are never written to the user file nor cancellable by the user.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from utils.utils import atomic_write, preserve_corrupt

_FILE_NAME = "scheduled_tasks.json"
_USER_PREFIX = "user"
_SYSTEM_PREFIX = "system"

# Callback signatures
DeliverCb = Callable[["ScheduledTask"], Awaitable[None]]
RunAgentCb = Callable[[str, str, "list[str] | None"], Awaitable[None]]


@dataclass
class ScheduledTask:
    """A single chat-created (or system-seeded) periodic task."""

    task_id: str
    schedule: str  # cron expression, e.g. "0 8 * * *"
    prompt: str  # instruction injected to the agent when fired
    session_key: str
    channel: str | None = None  # None = internal side-effect (no push)
    skills: list[str] | None = None
    system: bool = False  # True = built-in, protected from user cancel
    created_at: str = ""

    @property
    def job_name(self) -> str:
        """The CronScheduler job name for this task."""
        prefix = _SYSTEM_PREFIX if self.system else _USER_PREFIX
        return f"{prefix}:{self.task_id}"


class ScheduledTaskService:
    """CRUD + persistence + firing for scheduled agent tasks."""

    def __init__(
        self,
        workspace: str | Path,
        cron: Any,  # CronScheduler (register_job / unregister_job)
        *,
        deliver: DeliverCb | None = None,
        run_agent: RunAgentCb | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._file = self._workspace / _FILE_NAME
        self._cron = cron
        self._deliver = deliver
        self._run_agent = run_agent
        self._tasks: dict[str, ScheduledTask] = {}

    # -- callback wiring (set by the entry point / orchestrator) --------------

    def set_deliver(self, deliver: DeliverCb) -> None:
        self._deliver = deliver

    def set_run_agent(self, run_agent: RunAgentCb) -> None:
        self._run_agent = run_agent

    # -- CRUD -----------------------------------------------------------------

    def add_task(
        self,
        *,
        session_key: str,
        channel: str | None,
        schedule: str,
        prompt: str,
        skills: list[str] | None = None,
    ) -> ScheduledTask:
        """Create, persist, and register a new user task.

        Raises ``ValueError`` (from the cron scheduler) if *schedule* is not a
        valid cron expression.
        """
        task = ScheduledTask(
            task_id=uuid.uuid4().hex[:8],
            schedule=schedule,
            prompt=prompt,
            session_key=session_key,
            channel=channel,
            skills=skills,
            system=False,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        # Register first so an invalid cron raises before we persist.
        self._cron.register_job(task.job_name, schedule=schedule)
        self._tasks[task.task_id] = task
        self._save()
        logger.info("Scheduled task created: {} ({})", task.task_id, schedule)
        return task

    def seed_system_task(
        self,
        *,
        task_id: str,
        schedule: str,
        prompt: str,
        session_key: str,
        skills: list[str] | None = None,
    ) -> ScheduledTask:
        """Register a built-in system task (idempotent, not persisted)."""
        task = ScheduledTask(
            task_id=task_id,
            schedule=schedule,
            prompt=prompt,
            session_key=session_key,
            channel=None,
            skills=skills,
            system=True,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._cron.register_job(task.job_name, schedule=schedule)
        self._tasks[task_id] = task
        return task

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """Cancel a user task.  Returns ``(ok, message)``."""
        task = self._tasks.get(task_id)
        if task is None:
            return False, f"未找到任务 {task_id}"
        if task.system:
            return False, f"任务 {task_id} 是内置任务，不能取消"
        self._cron.unregister_job(task.job_name)
        self._tasks.pop(task_id, None)
        self._save()
        logger.info("Scheduled task cancelled: {}", task_id)
        return True, f"已取消任务 {task_id}"

    def list_tasks(self, session_key: str | None = None) -> list[ScheduledTask]:
        """List tasks, optionally filtered to a single session."""
        tasks = list(self._tasks.values())
        if session_key is not None:
            tasks = [t for t in tasks if t.session_key == session_key]
        return tasks

    def find_by_keyword(
        self, keyword: str, session_key: str | None = None
    ) -> list[ScheduledTask]:
        """Find tasks whose prompt contains *keyword* (case-insensitive)."""
        kw = keyword.lower()
        return [t for t in self.list_tasks(session_key) if kw in t.prompt.lower()]

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    # -- firing ---------------------------------------------------------------

    async def fire(self, job_name: str) -> None:
        """Execute the task behind *job_name* (``"user:<id>"`` / ``"system:<id>"``)."""
        _, _, task_id = job_name.partition(":")
        task = self._tasks.get(task_id)
        if task is None:
            logger.warning("Scheduled fire: unknown task {!r}", job_name)
            return

        if task.channel:
            if self._deliver is None:
                logger.warning(
                    "Scheduled fire: no deliver callback for push task {}", task_id
                )
                return
            await self._deliver(task)
        else:
            if self._run_agent is None:
                logger.warning(
                    "Scheduled fire: no run_agent callback for internal task {}",
                    task_id,
                )
                return
            await self._run_agent(task.session_key, task.prompt, task.skills)

    # -- persistence ----------------------------------------------------------

    def load(self) -> None:
        """Load persisted user tasks and re-register them with the cron scheduler."""
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Preserve the corrupt file instead of letting the next _save()
            # overwrite (and permanently lose) potentially recoverable tasks.
            backup = preserve_corrupt(self._file)
            logger.warning(
                "Scheduled tasks file corrupt, preserved at {} — starting with "
                "no user tasks; inspect the backup to recover.",
                backup,
            )
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            try:
                task = ScheduledTask(**item)
                task.system = False  # user file only ever holds user tasks
                self._cron.register_job(task.job_name, schedule=task.schedule)
                self._tasks[task.task_id] = task
            except (TypeError, ValueError):
                logger.warning("Skipping invalid scheduled task entry: {!r}", item)

    def _save(self) -> None:
        """Persist non-system tasks atomically and durably (fsync + rename)."""
        user_tasks = [asdict(t) for t in self._tasks.values() if not t.system]
        atomic_write(
            self._file,
            json.dumps(user_tasks, ensure_ascii=False, indent=2),
        )
