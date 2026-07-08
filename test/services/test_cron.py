"""Tests for CronScheduler."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from services.cron import CronJob, CronScheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def cron(state_dir):
    return CronScheduler(state_dir)


# ---------------------------------------------------------------------------
# register_job
# ---------------------------------------------------------------------------


class TestRegisterJob:
    def test_register_single(self, cron):
        job = cron.register_job("dream", interval_hours=2)
        assert job.name == "dream"
        assert job.interval_hours == 2
        assert job.next_run_at_ms > 0
        assert "dream" in cron._jobs

    def test_register_idempotent(self, cron):
        cron.register_job("dream", interval_hours=2)
        j2 = cron.register_job("dream", interval_hours=4)
        assert j2.interval_hours == 4
        assert len(cron._jobs) == 1

    def test_register_multiple(self, cron):
        cron.register_job("dream", interval_hours=2)
        cron.register_job("cleanup", interval_hours=24)
        assert len(cron._jobs) == 2
        assert "dream" in cron._jobs
        assert "cleanup" in cron._jobs


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_arms_timer(self, cron):
        cron.register_job("dream", interval_hours=2)
        cron.stop()  # cancel the auto-started loop
        assert cron._running is False

    def test_stop_cancels_timer(self, cron):
        cron.register_job("dream", interval_hours=2)
        assert cron._running is True
        cron.stop()
        assert cron._running is False
        assert cron._timer_task is None

    def test_stop_idempotent(self, cron):
        cron.stop()
        cron.stop()
        assert cron._running is False


# ---------------------------------------------------------------------------
# Timer firing
# ---------------------------------------------------------------------------


class TestTimerFires:
    @pytest.mark.asyncio
    async def test_job_fires_after_interval(self, state_dir):
        """Job fires when its interval elapses."""
        fired: list[str] = []

        async def on_job(name: str) -> None:
            fired.append(name)

        cron = CronScheduler(state_dir, on_job=on_job)
        # Register with a very short interval
        cron.register_job("test", interval_hours=0.001)  # ~3.6 seconds
        # Force immediate run by setting next_run_at_ms in the past
        cron._jobs["test"].next_run_at_ms = 1  # 1ms after epoch = already due
        cron._arm_timer()

        # Wait for the timer to fire
        for _ in range(20):
            if fired:
                break
            await asyncio.sleep(0.05)

        cron.stop()
        assert "test" in fired

    @pytest.mark.asyncio
    async def test_ensure_loop_auto_starts(self, state_dir):
        """Registering the first job starts the background loop."""
        cron = CronScheduler(state_dir)
        assert cron._running is False
        cron.register_job("dream", interval_hours=2)
        assert cron._running is True
        cron.stop()

    @pytest.mark.asyncio
    async def test_concurrent_execution_dedup(self, state_dir):
        """Per-job lock prevents overlapping runs."""
        running = 0
        max_concurrent = 0

        async def on_job(name: str) -> None:
            nonlocal running, max_concurrent
            running += 1
            max_concurrent = max(max_concurrent, running)
            await asyncio.sleep(0.1)
            running -= 1

        cron = CronScheduler(state_dir, on_job=on_job)
        job = cron._jobs.setdefault(
            "test",
            CronJob(name="test", interval_hours=1),
        )
        cron._running = True

        # Fire twice in rapid succession — second should skip
        t1 = asyncio.create_task(cron._execute_job(job))
        t2 = asyncio.create_task(cron._execute_job(job))
        await asyncio.gather(t1, t2)

        assert max_concurrent == 1  # second call was skipped by lock


# ---------------------------------------------------------------------------
# run_job_now
# ---------------------------------------------------------------------------


class TestRunJobNow:
    @pytest.mark.asyncio
    async def test_run_job_now_fires_callback(self, state_dir):
        fired: list[str] = []

        async def on_job(name: str) -> None:
            fired.append(name)

        cron = CronScheduler(state_dir, on_job=on_job)
        cron._running = True
        cron.register_job("dream", interval_hours=2)

        result = await cron.run_job_now("dream")
        assert result is True
        assert "dream" in fired

    @pytest.mark.asyncio
    async def test_run_job_now_unknown(self, cron):
        result = await cron.run_job_now("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_save_and_load_state(self, state_dir):
        cron1 = CronScheduler(state_dir)
        cron1._running = True
        cron1.register_job("dream", interval_hours=2)
        cron1._jobs["dream"].last_run_at_ms = 1234567890
        cron1._jobs["dream"].last_status = "ok"
        cron1._save_state()
        cron1.stop()

        # Re-create — should restore last_run
        cron2 = CronScheduler(state_dir)
        job = cron2.register_job("dream", interval_hours=2)
        assert job.last_run_at_ms == 1234567890
        assert job.last_status == "ok"

    def test_load_empty_state(self, state_dir):
        cron = CronScheduler(state_dir)
        state = cron._load_state()
        assert state == {}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_register_before_start(self, cron):
        """Registration auto-starts via _ensure_loop."""
        job = cron.register_job("dream", interval_hours=2)
        assert cron._running is True
        assert job.interval_hours == 2

    def test_get_next_wake_none_when_no_jobs(self, cron):
        assert cron._get_next_wake_ms() is None


# ---------------------------------------------------------------------------
# Cron-expression schedules (croniter)
# ---------------------------------------------------------------------------


class TestCronSchedule:
    def test_register_with_cron_schedule(self, cron):
        job = cron.register_job("daily8", schedule="0 8 * * *")
        assert job.schedule == "0 8 * * *"
        assert job.next_run_at_ms > 0
        cron.stop()

    def test_cron_next_run_is_future(self, cron):
        from services.cron import _now_ms

        job = cron.register_job("daily8", schedule="0 8 * * *")
        assert job.next_run_at_ms > _now_ms()
        cron.stop()

    def test_cron_every_minute_within_60s(self, cron):
        from services.cron import _now_ms

        job = cron.register_job("everymin", schedule="* * * * *")
        delta_ms = job.next_run_at_ms - _now_ms()
        assert 0 < delta_ms <= 60_000
        cron.stop()

    def test_invalid_cron_raises(self, cron):
        with pytest.raises(ValueError):
            cron.register_job("bad", schedule="not a cron")
        cron.stop()

    def test_cron_takes_precedence_over_interval(self, cron):
        """When both are given, the cron schedule wins."""
        job = cron.register_job("both", interval_hours=2, schedule="0 8 * * *")
        assert job.schedule == "0 8 * * *"
        cron.stop()


# ---------------------------------------------------------------------------
# unregister_job
# ---------------------------------------------------------------------------


class TestUnregisterJob:
    def test_unregister_removes_job(self, cron):
        cron.register_job("temp", schedule="0 8 * * *")
        assert "temp" in cron._jobs
        cron.unregister_job("temp")
        assert "temp" not in cron._jobs
        cron.stop()

    def test_unregister_unknown_is_noop(self, cron):
        cron.unregister_job("nonexistent")  # must not raise
        cron.stop()

    def test_unregister_drops_persisted_state(self, state_dir):
        cron1 = CronScheduler(state_dir)
        cron1._running = True
        cron1.register_job("temp", schedule="0 8 * * *")
        cron1._jobs["temp"].last_run_at_ms = 999
        cron1._save_state()
        cron1.unregister_job("temp")
        cron1.stop()

        # A fresh scheduler must not resurrect the removed job's state
        assert "temp" not in cron1._load_state()
