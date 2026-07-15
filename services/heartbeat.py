"""HeartbeatService — periodic agent task executor from HEARTBEAT.md.

Every 30 minutes, reads ``{workspace}/HEARTBEAT.md``, finds unchecked
tasks under ``## Active Tasks``, runs each through the agent pipeline,
and moves completed tasks to ``## Completed``.

The workspace copy is created from ``prompt_templates/HEARTBEAT.md`` on
first launch.  AGENTS.md already instructs the agent to manage this file.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from utils import render_template
from utils.utils import atomic_write

ACTIVE_HEADING = "## Active Tasks"
COMPLETED_HEADING = "## Completed"
SESSION_KEY = "system:heartbeat"

# Callback: (session_key, prompt, skills) → None
_RunAgent = Callable[[str, str, list[str] | None], Awaitable[None]]


class HeartbeatService:
    """Reads HEARTBEAT.md and executes active tasks via the agent pipeline.

    Parameters
    ----------
    workspace:
        Root directory where ``HEARTBEAT.md`` lives (or will be created).
    run_agent:
        Callback ``(session_key, prompt, skills)`` that executes a task
        through the orchestrator's ``process_message()``.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        run_agent: _RunAgent | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._heartbeat_file = self._workspace / "HEARTBEAT.md"
        self._run_agent = run_agent

    # -- lifecycle -----------------------------------------------------------

    def ensure_file(self) -> None:
        """Create HEARTBEAT.md from the prompt template if missing."""
        if self._heartbeat_file.exists():
            return
        content = render_template("HEARTBEAT.md")
        self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._heartbeat_file, content)
        logger.info("Heartbeat: created workspace copy at {}", self._heartbeat_file)

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        """Execute one heartbeat cycle."""
        if self._run_agent is None:
            return

        content = self._read_file()
        tasks = self._parse_active_tasks(content)
        if not tasks:
            logger.debug("Heartbeat: no active tasks, skipping")
            return

        logger.info("Heartbeat: executing {} task(s)", len(tasks))

        completed: list[str] = []
        for task in tasks:
            try:
                await self._run_agent(SESSION_KEY, task, None)
                completed.append(task)
                logger.info("Heartbeat: task completed: {!r}", task)
            except Exception:
                logger.exception("Heartbeat: task failed: {!r}", task)

        if completed:
            self._move_to_completed(completed)

    # -- file parsing --------------------------------------------------------

    @staticmethod
    def _parse_active_tasks(content: str) -> list[str]:
        """Return the description of every ``- [ ]`` task under
        ``## Active Tasks``."""
        tasks: list[str] = []
        in_active = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(COMPLETED_HEADING):
                break
            if stripped == ACTIVE_HEADING:
                in_active = True
                continue
            if in_active and stripped.startswith("- [ ]"):
                desc = stripped[5:].strip()
                if desc:
                    tasks.append(desc)
        return tasks

    def _move_to_completed(self, task_descriptions: list[str]) -> None:
        """Move completed tasks from ``## Active Tasks`` to ``## Completed``.

        Re-reads the file so concurrent agent edits are preserved.
        """
        content = self._read_file()
        lines = content.splitlines()
        remaining: list[str] = []
        completed_lines: list[str] = []

        in_active = False
        in_completed = False

        for line in lines:
            stripped = line.strip()
            if stripped == ACTIVE_HEADING:
                in_active = True
                in_completed = False
                remaining.append(line)
                continue
            if stripped.startswith(COMPLETED_HEADING):
                in_active = False
                in_completed = True
                remaining.append(line)
                continue

            if in_active and stripped.startswith("- [ ]"):
                desc = stripped[5:].strip()
                if desc in task_descriptions:
                    completed_lines.append(f"- [x] {desc}")
                    continue

            if in_completed and stripped.startswith("- [x]"):
                continue

            remaining.append(line)

        # Append completed items after ## Completed heading
        insert_at = 0
        for i, line in enumerate(remaining):
            if line.strip().startswith(COMPLETED_HEADING):
                insert_at = i + 1
                break

        if insert_at == 0:
            remaining.append("")
            remaining.append(COMPLETED_HEADING)
            remaining.append("")
            insert_at = len(remaining)

        for cl in reversed(completed_lines):
            remaining.insert(insert_at, cl)

        atomic_write(self._heartbeat_file, "\n".join(remaining) + "\n")
        logger.info(
            "Heartbeat: archived {} task(s) to ## Completed",
            len(completed_lines),
        )

    # -- helpers -------------------------------------------------------------

    def _read_file(self) -> str:
        try:
            return self._heartbeat_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
