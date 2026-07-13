"""Xiaohongshu turtle-soup daily-publish prompt.

The recurring publish workflow is now a *system* scheduled task
(:mod:`services.scheduled_tasks`).  This module only holds the two pieces
the orchestrator needs to seed that task: the session key it runs under and
the instruction prompt injected to the agent when it fires.

The agent does all the work: read state → generate both 汤面 + 汤底 →
save to output files → update state → publish as a two-image post.
State and output files live under ``{workspace}/xiaohongshu/``, created
lazily by the agent's ``write`` tool.
"""

from __future__ import annotations

from pathlib import Path

XIAOHONGSHU_SESSION_KEY = "xiaohongshu"

_TRIGGER_TEMPLATE = (
    "请执行每日海龟汤发布流程（同一篇笔记发两张图，第一张汤面，第二张汤底）：\n"
    "\n"
    "1. 读取 {workspace}/xiaohongshu/state.json 获取当前 series 编号。\n"
    "2. 构思一个新的海龟汤谜题，生成：\n"
    "   - 汤面：谜面/故事（200-500 字，引人入胜，小红书风格）\n"
    "   - 汤底：答案/真相（300-600 字，反转震撼，解释推理逻辑）\n"
    "3. 保存内容到输出文件（{workspace}/xiaohongshu/<date>_tangmian.md 和 <date>_tangdi.md）。\n"
    "4. 更新 state.json：series 编号 +1。\n"
    "5. 调用 xiaohongshu_publish 发布：\n"
    "   - title: \"海龟汤 #{{series}}\"\n"
    "   - content: 汤面全文\n"
    "   - content2: 汤底全文\n"
    "   - caption: \"左滑查看答案，评论区留下你的推理🐢 #海龟汤 #推理 #悬疑\"\n"
    "\n"
    "重要：content 和 content2 都要提供，这样脚本会生成两张图片——第一张显示汤面，第二张显示汤底。"
)


def xiaohongshu_prompt(workspace: str | Path) -> str:
    """Build the daily-publish instruction bound to *workspace*."""
    return _TRIGGER_TEMPLATE.format(workspace=Path(workspace))
