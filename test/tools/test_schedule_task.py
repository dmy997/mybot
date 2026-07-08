"""Tests for ScheduleTaskTool."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.session_context import SessionContext, reset, set_current
from services.scheduled_tasks import ScheduledTask
from tools.schedule_task import ScheduleTaskTool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    s = MagicMock()
    s.add_task = MagicMock()
    s.list_tasks = MagicMock(return_value=[])
    s.cancel = MagicMock(return_value=(True, "done"))
    s.find_by_keyword = MagicMock(return_value=[])
    return s


@pytest.fixture
def tool(service):
    return ScheduleTaskTool(service)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_task_via_service(self, tool, service):
        service.add_task.return_value = ScheduledTask(
            task_id="a1b2", schedule="0 8 * * *", prompt="p",
            session_key="s1", channel="wechat",
        )
        token = set_current(SessionContext("s1", "wechat"))
        try:
            result = await tool.execute(action="create", cron="0 8 * * *", task="p")
        finally:
            reset(token)
        assert result.success
        assert "a1b2" in result.content
        service.add_task.assert_called_once_with(
            session_key="s1", channel="wechat", schedule="0 8 * * *", prompt="p",
        )

    @pytest.mark.asyncio
    async def test_missing_session_context(self, tool):
        result = await tool.execute(action="create", cron="0 8 * * *", task="p")
        assert not result.success
        assert "会话" in result.error

    @pytest.mark.asyncio
    async def test_missing_cron(self, tool):
        token = set_current(SessionContext("s1", "wechat"))
        try:
            result = await tool.execute(action="create", task="p")
        finally:
            reset(token)
        assert not result.success

    @pytest.mark.asyncio
    async def test_missing_task(self, tool):
        token = set_current(SessionContext("s1", "wechat"))
        try:
            result = await tool.execute(action="create", cron="0 8 * * *")
        finally:
            reset(token)
        assert not result.success

    @pytest.mark.asyncio
    async def test_invalid_cron(self, tool, service):
        service.add_task.side_effect = ValueError("bad expr")
        token = set_current(SessionContext("s1", "wechat"))
        try:
            result = await tool.execute(action="create", cron="x x x", task="p")
        finally:
            reset(token)
        assert not result.success


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    @pytest.mark.asyncio
    async def test_empty_list(self, tool, service):
        service.list_tasks.return_value = []
        result = await tool.execute(action="list")
        assert result.success

    @pytest.mark.asyncio
    async def test_list_shows_tasks(self, tool, service):
        service.list_tasks.return_value = [
            ScheduledTask(task_id="a", schedule="0 8 * * *", prompt="p1",
                          session_key="s1", channel="wechat"),
            ScheduledTask(task_id="b", schedule="0 9 * * *", prompt="p2",
                          session_key="s1", channel="wechat", system=True),
        ]
        result = await tool.execute(action="list")
        assert "a" in result.content
        assert "内置" in result.content


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_by_id(self, tool, service):
        result = await tool.execute(action="cancel", task_id="task123")
        assert result.success
        service.cancel.assert_called_once_with("task123")

    @pytest.mark.asyncio
    async def test_cancel_by_keyword(self, tool, service):
        service.find_by_keyword.return_value = [
            ScheduledTask(task_id="task123", schedule="0 8 * * *", prompt="前沿",
                          session_key="s1", channel="wechat"),
        ]
        result = await tool.execute(action="cancel", keyword="前沿")
        assert result.success

    @pytest.mark.asyncio
    async def test_cancel_id_not_found(self, tool, service):
        service.cancel.return_value = (False, "未找到任务 ghost")
        result = await tool.execute(action="cancel", task_id="ghost")
        assert not result.success

    @pytest.mark.asyncio
    async def test_both_missing(self, tool):
        result = await tool.execute(action="cancel")
        assert not result.success


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    @pytest.mark.asyncio
    async def test_rejects_unknown(self, tool):
        result = await tool.execute(action="nonexistent")
        assert not result.success
