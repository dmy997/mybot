"""schedule_task tool — let the agent create/list/cancel periodic tasks.

A single tool with an ``action`` parameter (``create`` / ``list`` / ``cancel``).
The LLM translates a natural-language request (e.g. "每天八点给我推送智能体前沿
进展") into a cron expression and a task instruction, then calls this tool.

The tool reads the *current* session/channel from
:mod:`core.session_context` so a created task knows where to run and push its
result — it never receives ``session_key`` through its arguments.
"""

from __future__ import annotations

from typing import Any

from core.session_context import get_current

from .tool import Tool, ToolResult


class ScheduleTaskTool(Tool):
    """Create, list, or cancel scheduled periodic tasks."""

    name = "schedule_task"
    _scopes = {"core"}  # main agent only; not delegated to sub-agents
    _parallel = False  # mutates scheduled-task state
    capabilities: set = set()
    description = (
        "管理定时周期任务。当用户要求'每天/每周/定时'做某事（例如'每天八点给我"
        "推送智能体前沿进展'）时，用 action='create' 创建任务；用 action='list' "
        "查看已有任务；用 action='cancel' 取消任务。\n\n"
        "cron 表达式为标准 5 段格式 '分 时 日 月 周'，例如：\n"
        "  '0 8 * * *'   每天 8:00\n"
        "  '30 8 * * 1'  每周一 8:30\n"
        "  '0 9 * * 1-5' 工作日 9:00\n"
        "  '*/15 * * * *' 每 15 分钟\n"
        "创建时把用户意图翻译成 cron 表达式和一句要执行的指令。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel"],
                "description": "create=新建, list=列出, cancel=取消",
            },
            "cron": {
                "type": "string",
                "description": "cron 表达式，如 '0 8 * * *'。action=create 时必填。",
            },
            "task": {
                "type": "string",
                "description": (
                    "到点要执行的指令，一句完整的中文，例如"
                    "'搜索并总结智能体领域的前沿进展，简洁推送给我'。"
                    "action=create 时必填。"
                ),
            },
            "task_id": {
                "type": "string",
                "description": "要取消的任务 ID。action=cancel 时提供。",
            },
            "keyword": {
                "type": "string",
                "description": "按关键词匹配要取消的任务（当不知道 ID 时）。action=cancel。",
            },
        },
        "required": ["action"],
    }

    def __init__(self, service: Any) -> None:  # ScheduledTaskService
        self._service = service

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).strip().lower()
        if action == "create":
            return self._create(kwargs)
        if action == "list":
            return self._list()
        if action == "cancel":
            return self._cancel(kwargs)
        return ToolResult(
            success=False, content="", error=f"未知 action: {action!r}"
        )

    # -- actions --------------------------------------------------------------

    def _create(self, kwargs: dict[str, Any]) -> ToolResult:
        ctx = get_current()
        if ctx is None or not ctx.session_key:
            return ToolResult(
                success=False, content="",
                error="无法确定当前会话，定时任务创建失败。",
            )
        cron = str(kwargs.get("cron", "")).strip()
        task = str(kwargs.get("task", "")).strip()
        if not cron:
            return ToolResult(success=False, content="", error="缺少 cron 表达式。")
        if not task:
            return ToolResult(success=False, content="", error="缺少 task 指令。")

        try:
            created = self._service.add_task(
                session_key=ctx.session_key,
                channel=ctx.source or None,
                schedule=cron,
                prompt=task,
            )
        except ValueError as exc:
            return ToolResult(
                success=False, content="", error=f"无效的 cron 表达式：{exc}"
            )

        return ToolResult(
            success=True,
            content=f"✅ 已创建定时任务 {created.task_id}：cron `{cron}` 执行「{task}」",
        )

    def _list(self) -> ToolResult:
        ctx = get_current()
        session_key = ctx.session_key if ctx else None
        tasks = self._service.list_tasks(session_key=session_key)
        if not tasks:
            return ToolResult(success=True, content="当前没有定时任务。")
        lines = ["当前定时任务："]
        for t in tasks:
            tag = "（内置）" if t.system else ""
            lines.append(f"  • {t.task_id}{tag} | `{t.schedule}` | {t.prompt}")
        return ToolResult(success=True, content="\n".join(lines))

    def _cancel(self, kwargs: dict[str, Any]) -> ToolResult:
        task_id = str(kwargs.get("task_id", "")).strip()
        keyword = str(kwargs.get("keyword", "")).strip()

        if not task_id and keyword:
            ctx = get_current()
            session_key = ctx.session_key if ctx else None
            matches = self._service.find_by_keyword(keyword, session_key=session_key)
            if not matches:
                return ToolResult(
                    success=False, content="", error=f"没有匹配「{keyword}」的任务。"
                )
            if len(matches) > 1:
                ids = ", ".join(m.task_id for m in matches)
                return ToolResult(
                    success=False, content="",
                    error=f"匹配到多个任务（{ids}），请用 task_id 精确取消。",
                )
            task_id = matches[0].task_id

        if not task_id:
            return ToolResult(
                success=False, content="", error="请提供 task_id 或 keyword。"
            )

        ok, msg = self._service.cancel(task_id)
        return ToolResult(success=ok, content=msg if ok else "", error=None if ok else msg)
