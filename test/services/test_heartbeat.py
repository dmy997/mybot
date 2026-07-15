"""Tests for HeartbeatService."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services.heartbeat import (
    ACTIVE_HEADING,
    COMPLETED_HEADING,
    SESSION_KEY,
    HeartbeatService,
)


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def heartbeat(tmp_workspace):
    return HeartbeatService(workspace=tmp_workspace)


# -- _parse_active_tasks ---------------------------------------------------


def test_parse_empty(heartbeat):
    assert heartbeat._parse_active_tasks("") == []


def test_parse_no_active_tasks(heartbeat):
    content = f"{ACTIVE_HEADING}\n\n## Other\n\nSome text\n"
    assert heartbeat._parse_active_tasks(content) == []


def test_parse_single_task(heartbeat):
    content = f"{ACTIVE_HEADING}\n- [ ] Write tests for auth module\n"
    assert heartbeat._parse_active_tasks(content) == ["Write tests for auth module"]


def test_parse_multiple_tasks(heartbeat):
    content = (
        f"{ACTIVE_HEADING}\n"
        "- [ ] Check server health\n"
        "- [ ] Review pull requests\n"
        "- [ ] Update dependencies\n"
    )
    assert heartbeat._parse_active_tasks(content) == [
        "Check server health",
        "Review pull requests",
        "Update dependencies",
    ]


def test_parse_stops_at_completed(heartbeat):
    content = (
        f"{ACTIVE_HEADING}\n"
        "- [ ] Active task\n"
        f"{COMPLETED_HEADING}\n"
        "- [ ] This is after completed, should be ignored\n"
    )
    assert heartbeat._parse_active_tasks(content) == ["Active task"]


def test_parse_skips_empty_checkbox(heartbeat):
    content = f"{ACTIVE_HEADING}\n- [ ]   \n"
    assert heartbeat._parse_active_tasks(content) == []


def test_parse_skips_checked_tasks(heartbeat):
    content = f"{ACTIVE_HEADING}\n- [x] Done task\n- [ ] Active task\n"
    assert heartbeat._parse_active_tasks(content) == ["Active task"]


# -- _move_to_completed ----------------------------------------------------


def test_move_single_task_to_completed(heartbeat, tmp_workspace):
    content = (
        f"{ACTIVE_HEADING}\n"
        "- [ ] Deploy to production\n"
        f"\n{COMPLETED_HEADING}\n"
    )
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(content)
    heartbeat._move_to_completed(["Deploy to production"])

    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert "- [x] Deploy to production" in result
    assert "- [ ] Deploy to production" not in result


def test_move_multiple_tasks(heartbeat, tmp_workspace):
    content = (
        f"{ACTIVE_HEADING}\n"
        "- [ ] Task A\n"
        "- [ ] Task B\n"
        "- [ ] Task C\n"
        f"\n{COMPLETED_HEADING}\n"
    )
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(content)
    heartbeat._move_to_completed(["Task A", "Task C"])

    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert "- [x] Task A" in result
    assert "- [x] Task C" in result
    assert "- [ ] Task B" in result  # not completed
    assert "- [ ] Task A" not in result
    assert "- [ ] Task C" not in result


def test_move_creates_completed_section_if_missing(heartbeat, tmp_workspace):
    content = f"{ACTIVE_HEADING}\n- [ ] Write docs\n"
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(content)
    heartbeat._move_to_completed(["Write docs"])

    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert COMPLETED_HEADING in result
    assert "- [x] Write docs" in result


def test_move_preserves_other_content(heartbeat, tmp_workspace):
    content = (
        "# HEARTBEAT\n\n"
        "Some preamble text\n\n"
        f"{ACTIVE_HEADING}\n"
        "- [ ] Do the thing\n"
        f"\n{COMPLETED_HEADING}\n"
        "- [x] Old completed\n"
        "\n## Other Section\n"
        "Keep this content\n"
    )
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(content)
    heartbeat._move_to_completed(["Do the thing"])

    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert "Some preamble text" in result
    assert "## Other Section" in result
    assert "Keep this content" in result
    assert "- [x] Do the thing" in result


# -- ensure_file -----------------------------------------------------------


def test_ensure_file_creates_from_template(tmp_workspace):
    hb = HeartbeatService(workspace=tmp_workspace)
    hb.ensure_file()
    f = tmp_workspace / "HEARTBEAT.md"
    assert f.exists()
    content = f.read_text()
    assert ACTIVE_HEADING in content
    assert COMPLETED_HEADING in content


def test_ensure_file_noop_if_exists(tmp_workspace):
    f = tmp_workspace / "HEARTBEAT.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("custom content")
    hb = HeartbeatService(workspace=tmp_workspace)
    hb.ensure_file()
    assert f.read_text() == "custom content"


# -- run -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_skips_when_no_tasks(heartbeat, tmp_workspace):
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(
        f"{ACTIVE_HEADING}\n\n{COMPLETED_HEADING}\n"
    )
    mock_agent = AsyncMock()
    heartbeat._run_agent = mock_agent
    await heartbeat.run()
    mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_run_executes_tasks(tmp_workspace):
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(
        f"{ACTIVE_HEADING}\n- [ ] Task 1\n- [ ] Task 2\n\n{COMPLETED_HEADING}\n"
    )
    mock_agent = AsyncMock()
    hb = HeartbeatService(workspace=tmp_workspace, run_agent=mock_agent)
    await hb.run()

    assert mock_agent.call_count == 2
    mock_agent.assert_any_call(SESSION_KEY, "Task 1", None)
    mock_agent.assert_any_call(SESSION_KEY, "Task 2", None)

    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert "- [x] Task 1" in result
    assert "- [x] Task 2" in result


@pytest.mark.asyncio
async def test_run_task_failure_stays_active(tmp_workspace):
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(
        f"{ACTIVE_HEADING}\n- [ ] Good task\n- [ ] Bad task\n\n{COMPLETED_HEADING}\n"
    )
    call_count = 0

    async def flaky_agent(_sk, prompt, _sl):
        nonlocal call_count
        call_count += 1
        if prompt == "Bad task":
            raise RuntimeError("simulated failure")

    hb = HeartbeatService(workspace=tmp_workspace, run_agent=flaky_agent)
    await hb.run()

    assert call_count == 2
    result = tmp_workspace.joinpath("HEARTBEAT.md").read_text()
    assert "- [x] Good task" in result
    assert "- [ ] Bad task" in result  # still active


@pytest.mark.asyncio
async def test_run_no_run_agent_is_noop(tmp_workspace):
    tmp_workspace.joinpath("HEARTBEAT.md").write_text(
        f"{ACTIVE_HEADING}\n- [ ] Some task\n"
    )
    hb = HeartbeatService(workspace=tmp_workspace)  # no run_agent
    await hb.run()
    # Should not raise — just return silently


# -- _read_file ------------------------------------------------------------


def test_read_file_missing_returns_empty(heartbeat):
    assert heartbeat._read_file() == ""
