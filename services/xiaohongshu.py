"""Xiaohongshu cron trigger — sends daily publishing instruction to mybot agent.

Triggered by CronScheduler every 24 hours. Instead of calling LLM directly,
it injects a message into the agent pipeline so the ``xiaohongshu`` skill
handles the full workflow (read state → generate content → publish).

State and output files live under ``{workspace}/xiaohongshu/``, managed
entirely by the agent via the ``read``/``write`` tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from utils import ensure_dir

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator

_SESSION_KEY = "xiaohongshu"
_TRIGGER_MESSAGE = (
    "请执行每日海龟汤发布流程："
    "检查 {workspace}/xiaohongshu/state.json 判断当前阶段（tangmian/tangdi），"
    "生成对应内容（汤面 200-500 字或汤底 300-600 字，小红书风格），"
    "保存到输出文件，更新状态，最后用 xiaohongshu_publish 工具发布到小红书。"
)


class XiaohongshuService:
    """Thin cron → agent bridge for the Xiaohongshu turtle-soup workflow.

    The agent does all the work; this service only delivers the instruction.
    """

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace)
        self._output_dir = ensure_dir(self._workspace / "xiaohongshu")

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    async def on_cron_trigger(self, orchestrator: Orchestrator) -> None:
        """Send the daily publishing instruction to the mybot agent."""
        message = _TRIGGER_MESSAGE.format(workspace=self._workspace)
        logger.info("Xiaohongshu cron: triggering agent workflow")
        try:
            await orchestrator.process_message(
                session_key=_SESSION_KEY,
                user_input=message,
                skills=["xiaohongshu"],
            )
        except Exception:
            logger.opt(exception=True).error(
                "Xiaohongshu cron: agent workflow failed"
            )
