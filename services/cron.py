"""CronScheduler — self-driven periodic job scheduler.

Runs an independent asyncio timer loop (nanobot-style ``_arm_timer``
pattern) that fires registered jobs at their configured intervals.
Does NOT depend on user input or external ``tick()`` calls — the timer
re-arms itself after each wake-up, forming a closed loop.

State is persisted to ``cron_state.json`` so last-run times survive
restarts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from croniter import croniter
from loguru import logger

from utils.utils import atomic_write

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SLEEP_MS = 300_000  # 5-minute cap — wake even with no pending jobs


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# CronJob
# ---------------------------------------------------------------------------


@dataclass
class CronJob:
    """Runtime state for a single scheduled job."""

    name: str
    interval_hours: float = 0.0
    schedule: str = ""  # cron expression; when set, overrides interval_hours
    next_run_at_ms: int = 0
    last_run_at_ms: int = 0
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None


# ---------------------------------------------------------------------------
# CronScheduler
# ---------------------------------------------------------------------------


class CronScheduler:
    """Self-driven periodic job scheduler with state persistence.

    Parameters
    ----------
    state_dir:
        Directory for ``cron_state.json``.
    on_job:
        Optional global callback invoked as ``on_job(job_name)`` when any
        job fires.  Use this to wire Dream or other system tasks.
    """

    def __init__(
        self,
        state_dir: Path,
        on_job: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ):
        self._state_file = Path(state_dir) / "cron_state.json"
        self.on_job = on_job
        self._jobs: dict[str, CronJob] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timer_task: asyncio.Task | None = None
        self._running = False

    # -- public API -----------------------------------------------------------

    def register_job(
        self,
        name: str,
        *,
        interval_hours: float | None = None,
        schedule: str = "",
    ) -> CronJob:
        """Register a periodic job (idempotent on re-registration).

        Provide either ``interval_hours`` (every N hours, relative to the
        last run) or ``schedule`` (a cron expression evaluated in local
        time).  When both are given the cron ``schedule`` wins.

        If the loop is already running the timer is re-armed so the new
        job's schedule takes effect immediately.
        """
        if schedule and not croniter.is_valid(schedule):
            raise ValueError(f"Invalid cron expression: {schedule!r}")

        job = CronJob(
            name=name,
            interval_hours=float(interval_hours) if interval_hours else 0.0,
            schedule=schedule,
        )

        # Restore persisted state so last-run time survives restarts
        existing = self._load_state().get(name)
        if existing:
            job.last_run_at_ms = existing.get("last_run_at_ms", 0)
            job.last_status = existing.get("last_status")
            job.last_error = existing.get("last_error")

        # Compute next run.
        if job.schedule:
            # Wall-clock cron: next occurrence after now (ignores last-run drift)
            job.next_run_at_ms = self._compute_next_run(job, _now_ms())
        elif job.last_run_at_ms > 0:
            job.next_run_at_ms = job.last_run_at_ms + int(job.interval_hours * 3600 * 1000)
        else:
            # First run: delay by the full interval so the user isn't
            # surprised by an immediate Dream on first launch.
            job.next_run_at_ms = _now_ms() + int(job.interval_hours * 3600 * 1000)

        self._jobs[name] = job
        self._ensure_loop()
        logger.info(
            "Cron: registered job {!r} ({}, next in {}s)",
            name,
            f"cron={job.schedule!r}" if job.schedule else f"every {job.interval_hours}h",
            max(0, (job.next_run_at_ms - _now_ms()) // 1000),
        )
        return job

    def unregister_job(self, name: str) -> None:
        """Remove a registered job and re-arm the timer.  Safe if absent."""
        if name not in self._jobs:
            return
        self._jobs.pop(name, None)
        self._locks.pop(name, None)
        self._save_state()  # persist the removal (drops stale last-run entry)
        if self._running:
            with suppress(RuntimeError):
                asyncio.get_running_loop()
                self._arm_timer()
        logger.info("Cron: unregistered job {!r}", name)

    def _compute_next_run(self, job: CronJob, after_ms: int) -> int:
        """Return the next fire time (ms) strictly after *after_ms*.

        Uses ``croniter`` in local time for cron ``schedule`` jobs, else
        ``interval_hours`` arithmetic.
        """
        if job.schedule:
            base = datetime.fromtimestamp(after_ms / 1000)
            nxt = croniter(job.schedule, base).get_next(datetime)
            return int(nxt.timestamp() * 1000)
        return after_ms + int(job.interval_hours * 3600 * 1000)

    async def start(self) -> None:
        """Start the timer loop (explicit, for re-start after ``stop()``)."""
        self._running = True
        self._arm_timer()
        logger.info("CronScheduler started with {} job(s)", len(self._jobs))

    def stop(self) -> None:
        """Cancel the timer task.  Safe to call multiple times."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    async def run_job_now(self, name: str) -> bool:
        """Manually trigger *name* immediately.  Returns True if the job exists."""
        job = self._jobs.get(name)
        if job is None:
            return False
        await self._execute_job(job)
        return True

    # -- timer loop (nanobot _arm_timer pattern) ------------------------------

    def _ensure_loop(self) -> None:
        """Start the background loop if not already running.

        When called from an async context the timer is armed immediately.
        From a sync context (e.g. tests) the loop is marked running but
        the timer is deferred until ``start()`` is called.
        """
        if not self._running:
            self._running = True
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # no event loop yet, start() will arm later
            self._arm_timer()

    def _get_next_wake_ms(self) -> int | None:
        times = [
            j.next_run_at_ms
            for j in self._jobs.values()
            if j.next_run_at_ms > 0
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick.

        Creates an ``asyncio.Task`` that sleeps until the earliest due job
        (capped at ``_MAX_SLEEP_MS``), then calls ``_on_timer`` which
        re-arms the timer — forming a perpetual self-scheduling loop.
        """
        if self._timer_task:
            self._timer_task.cancel()

        if not self._running:
            return

        next_wake = self._get_next_wake_ms()
        if next_wake is None:
            delay_ms = _MAX_SLEEP_MS
        else:
            delay_ms = min(_MAX_SLEEP_MS, max(0, next_wake - _now_ms()))
        delay_s = delay_ms / 1000

        async def tick() -> None:
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Fire due jobs, then re-arm."""
        now = _now_ms()
        due = [
            j
            for j in self._jobs.values()
            if j.next_run_at_ms > 0 and now >= j.next_run_at_ms
        ]

        for job in due:
            await self._execute_job(job)

        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job under a per-name lock (dedup).

        On failure, retries once immediately before marking the job as
        failed and scheduling the next run.
        """
        lock = self._locks.setdefault(job.name, asyncio.Lock())
        if lock.locked():
            logger.debug("Cron: job {!r} still running, skipping tick", job.name)
            return

        async with lock:
            start_ms = _now_ms()
            logger.info("Cron: executing job {!r}", job.name)

            ok = False
            last_exc: Exception | None = None
            for attempt in (1, 2):
                try:
                    if self.on_job:
                        await self.on_job(job.name)
                    ok = True
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == 1:
                        logger.warning(
                            "Cron: job {!r} failed (attempt 1), retrying once",
                            job.name,
                        )

            if ok:
                job.last_status = "ok"
                job.last_error = None
                logger.info("Cron: job {!r} completed", job.name)
            else:
                job.last_status = "error"
                job.last_error = str(last_exc)
                logger.exception("Cron: job {!r} failed after retry", job.name)

            job.last_run_at_ms = start_ms
            job.next_run_at_ms = self._compute_next_run(job, _now_ms())
            self._save_state()

    # -- persistence ----------------------------------------------------------

    def _load_state(self) -> dict:
        if not self._state_file.exists():
            return {}
        with suppress(ValueError, OSError):
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        return {}

    def _save_state(self) -> None:
        state: dict[str, dict] = {}
        for name, job in self._jobs.items():
            state[name] = {
                "last_run_at_ms": job.last_run_at_ms,
                "last_status": job.last_status,
                "last_error": job.last_error,
            }
        atomic_write(self._state_file, json.dumps(state, ensure_ascii=False))
