"""Tests for ScheduledTaskService — chat-created periodic tasks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.scheduled_tasks import ScheduledTask, ScheduledTaskService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def cron():
    """Mock CronScheduler — register/unregister are the only surfaces used."""
    m = MagicMock()
    m.register_job = MagicMock()
    m.unregister_job = MagicMock()
    return m


@pytest.fixture
def service(workspace, cron):
    return ScheduledTaskService(workspace, cron)


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------


class TestAddTask:
    def test_returns_task_with_id(self, service):
        task = service.add_task(
            session_key="wechat:private:@u", channel="wechat",
            schedule="0 8 * * *", prompt="push news",
        )
        assert isinstance(task, ScheduledTask)
        assert task.task_id
        assert task.channel == "wechat"
        assert task.system is False

    def test_registers_user_cron_job(self, service, cron):
        task = service.add_task(
            session_key="s1", channel="wechat",
            schedule="0 8 * * *", prompt="p",
        )
        cron.register_job.assert_called_once()
        args, kwargs = cron.register_job.call_args
        assert args[0] == f"user:{task.task_id}"
        assert kwargs["schedule"] == "0 8 * * *"

    def test_persists_to_disk(self, workspace, cron):
        svc = ScheduledTaskService(workspace, cron)
        svc.add_task(session_key="s1", channel="wechat",
                     schedule="0 8 * * *", prompt="p")
        data = json.loads((workspace / "scheduled_tasks.json").read_text())
        assert len(data) == 1
        assert data[0]["prompt"] == "p"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_removes_and_unregisters(self, service, cron):
        task = service.add_task(session_key="s1", channel="wechat",
                                schedule="0 8 * * *", prompt="p")
        ok, _ = service.cancel(task.task_id)
        assert ok is True
        cron.unregister_job.assert_called_once_with(f"user:{task.task_id}")
        assert service.list_tasks() == []

    def test_cancel_unknown_returns_false(self, service):
        ok, msg = service.cancel("nope")
        assert ok is False

    def test_cancel_system_rejected(self, service, cron):
        service.seed_system_task(task_id="xiaohongshu", schedule="0 20 * * *",
                                 prompt="post", session_key="xiaohongshu",
                                 skills=["xiaohongshu"])
        ok, msg = service.cancel("xiaohongshu")
        assert ok is False
        assert len(service.list_tasks()) == 1  # still present

    def test_cancel_persists_removal(self, workspace, cron):
        svc = ScheduledTaskService(workspace, cron)
        task = svc.add_task(session_key="s1", channel="wechat",
                            schedule="0 8 * * *", prompt="p")
        svc.cancel(task.task_id)
        data = json.loads((workspace / "scheduled_tasks.json").read_text())
        assert data == []


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    def test_filter_by_session(self, service):
        service.add_task(session_key="s1", channel="wechat",
                         schedule="0 8 * * *", prompt="a")
        service.add_task(session_key="s2", channel="wechat",
                         schedule="0 9 * * *", prompt="b")
        assert len(service.list_tasks()) == 2
        assert len(service.list_tasks(session_key="s1")) == 1

    def test_find_by_keyword(self, service):
        service.add_task(session_key="s1", channel="wechat",
                         schedule="0 8 * * *", prompt="推送智能体前沿进展")
        service.add_task(session_key="s1", channel="wechat",
                         schedule="0 9 * * *", prompt="天气预报")
        matches = service.find_by_keyword("前沿")
        assert len(matches) == 1
        assert "前沿" in matches[0].prompt


# ---------------------------------------------------------------------------
# fire
# ---------------------------------------------------------------------------


class TestFire:
    @pytest.mark.asyncio
    async def test_push_task_calls_deliver(self, service):
        deliver = AsyncMock()
        service.set_deliver(deliver)
        task = service.add_task(session_key="s1", channel="wechat",
                                schedule="0 8 * * *", prompt="p")
        await service.fire(f"user:{task.task_id}")
        deliver.assert_awaited_once()
        assert deliver.await_args.args[0].task_id == task.task_id

    @pytest.mark.asyncio
    async def test_internal_task_calls_run_agent(self, service):
        run_agent = AsyncMock()
        service.set_run_agent(run_agent)
        service.seed_system_task(task_id="xiaohongshu", schedule="0 20 * * *",
                                 prompt="post now", session_key="xiaohongshu",
                                 skills=["xiaohongshu"])
        await service.fire("system:xiaohongshu")
        run_agent.assert_awaited_once_with(
            "xiaohongshu", "post now", ["xiaohongshu"]
        )

    @pytest.mark.asyncio
    async def test_fire_unknown_is_noop(self, service):
        deliver = AsyncMock()
        service.set_deliver(deliver)
        await service.fire("user:ghost")  # must not raise
        deliver.assert_not_awaited()


# ---------------------------------------------------------------------------
# system tasks + persistence
# ---------------------------------------------------------------------------


class TestSystemAndPersistence:
    def test_seed_system_task_not_persisted(self, workspace, cron):
        svc = ScheduledTaskService(workspace, cron)
        svc.seed_system_task(task_id="xiaohongshu", schedule="0 20 * * *",
                             prompt="post", session_key="xiaohongshu")
        # System tasks are re-seeded from code, never written to the user JSON
        f = workspace / "scheduled_tasks.json"
        data = json.loads(f.read_text()) if f.exists() else []
        assert data == []

    def test_seed_registers_system_cron_job(self, service, cron):
        service.seed_system_task(task_id="xiaohongshu", schedule="0 20 * * *",
                                 prompt="post", session_key="xiaohongshu")
        args, kwargs = cron.register_job.call_args
        assert args[0] == "system:xiaohongshu"

    def test_load_restores_and_reregisters(self, workspace, cron):
        svc1 = ScheduledTaskService(workspace, cron)
        task = svc1.add_task(session_key="s1", channel="wechat",
                             schedule="0 8 * * *", prompt="p")
        # Fresh service loads persisted tasks
        cron2 = MagicMock()
        svc2 = ScheduledTaskService(workspace, cron2)
        svc2.load()
        loaded = svc2.list_tasks()
        assert len(loaded) == 1
        assert loaded[0].task_id == task.task_id
        cron2.register_job.assert_called_once()

    def test_load_preserves_corrupt_file(self, workspace, cron):
        """A corrupt tasks file is renamed aside, not silently dropped."""
        f = workspace / "scheduled_tasks.json"
        f.write_text("{ this is not valid json", encoding="utf-8")

        svc = ScheduledTaskService(workspace, cron)
        svc.load()  # must not raise

        # Original file is moved aside so its bytes survive for recovery
        assert not f.exists()
        backups = list(workspace.glob("scheduled_tasks.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{ this is not valid json"
        assert svc.list_tasks() == []

    def test_save_after_corrupt_does_not_clobber_backup(self, workspace, cron):
        """After a corrupt load, a later _save() writes fresh without losing the backup."""
        f = workspace / "scheduled_tasks.json"
        f.write_text("garbage{", encoding="utf-8")

        svc = ScheduledTaskService(workspace, cron)
        svc.load()
        svc.add_task(session_key="s1", channel="wechat",
                     schedule="0 8 * * *", prompt="fresh")

        # New file holds only the fresh task; the corrupt backup is untouched
        data = json.loads(f.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["prompt"] == "fresh"
        backups = list(workspace.glob("scheduled_tasks.json.corrupt-*"))
        assert backups and backups[0].read_text(encoding="utf-8") == "garbage{"
