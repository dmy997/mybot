"""Xiaohongshu turtle-soup daily-publish prompt.

The recurring publish workflow is now a *system* scheduled task
(:mod:`services.scheduled_tasks`).  This module only holds the two pieces
the orchestrator needs to seed that task: the session key it runs under and
the instruction prompt injected to the agent when it fires.

The agent does all the work through the ``xiaohongshu`` skill: read state →
generate content → publish.  State and output files live under
``{workspace}/xiaohongshu/``, created lazily by the agent's ``write`` tool.
"""

from __future__ import annotations

from pathlib import Path

XIAOHONGSHU_SESSION_KEY = "xiaohongshu"

_TRIGGER_TEMPLATE = (
    "请执行每日海龟汤发布流程："
    "检查 {workspace}/xiaohongshu/state.json 判断当前阶段（tangmian/tangdi），"
    "生成对应内容（汤面 200-500 字或汤底 300-600 字，小红书风格），"
    "保存到输出文件，更新状态，最后用 xiaohongshu_publish 工具发布到小红书。"
)


def xiaohongshu_prompt(workspace: str | Path) -> str:
    """Build the daily-publish instruction bound to *workspace*."""
    return _TRIGGER_TEMPLATE.format(workspace=Path(workspace))
