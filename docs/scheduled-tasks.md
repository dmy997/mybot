# 统一定时任务框架 (Scheduled Tasks)

## 概述

用户可以用自然语言创建周期任务——例如输入"每天八点给我推送智能体前沿进展"，
agent 就会把它翻译成 cron 表达式，注册一个定时任务，到点自动执行并把结果推送回
用户所在的聊天渠道。同一套框架也承载**系统内置任务**（如小红书海龟汤发帖）。

框架建立在既有的 [`CronScheduler`](dream-scheduler.md) 之上，核心抽象是
`ScheduledTaskService`：**"在 cron 时刻 T，用 prompt P 在 session K 里跑一次 agent"**。

## 分层架构

```
CronScheduler (services/cron.py) — 自驱动定时器原语（cron 表达式 via croniter）
  ├─ "dream"                    → 原生方法 dream.run()，不走 agent，保持不变
  └─ ScheduledTaskService (services/scheduled_tasks.py)
       ├─ user 任务   (channel 有值)  → 注入 MessageBus → outbound(channel) → consumer 推送给用户
       └─ system 任务 (channel=None) → run_agent 回调直接跑 process_message，丢弃输出（小红书）
```

push 任务和 side-effect 任务唯一的区别是 **fire 时的投递步骤**，被干净地隔离在
`fire()` 内，抽象不外泄。

## 两种任务

| 维度 | user 任务（push） | system 任务（side-effect） |
|------|------------------|---------------------------|
| **创建方式** | 聊天中 `schedule_task` 工具 | 代码 `seed_system_task()` 启动时 seed |
| **`channel`** | 来源渠道（`wechat` / `http` …） | `None` |
| **投递** | `deliver` 回调 → 注入 bus → 推送给用户 | `run_agent` 回调 → `process_message`，丢弃输出 |
| **持久化** | `scheduled_tasks.json` | 不持久化，每次启动从代码 re-seed |
| **cron job 名** | `user:{task_id}` | `system:{task_id}` |
| **用户可取消** | 是 | 否（`system=True` 保护） |

## 数据模型

```python
@dataclass
class ScheduledTask:
    task_id: str          # 短 uuid hex，如 "a1b2"
    schedule: str         # cron 表达式，如 "0 8 * * *"
    prompt: str           # 到点注入 agent 的指令
    session_key: str      # 在哪个 session 跑 / 推送到哪
    channel: str | None   # None=内部副作用；否则=来源渠道
    skills: list[str] | None = None
    system: bool = False  # True=内置，禁止用户取消
    created_at: str = ""

    @property
    def job_name(self) -> str:      # "user:a1b2" / "system:xiaohongshu"
        prefix = "system" if self.system else "user"
        return f"{prefix}:{self.task_id}"
```

user 任务持久化到 `{workspace}/scheduled_tasks.json`（原子 `fsync` + tmp + `os.replace` + 父目录 `fsync`，
只写非 system 任务）；启动时 `load()` 重新注册 cron job。若 JSON 损坏，文件被重命名为
`.corrupt-<ts>` 副本保存，拒绝用空列表覆盖——防止静默丢失所有用户任务
（借鉴 [nanobot](../nanobot/nanobot/nanobot/cron/service.py#L162)）。

## schedule_task 工具

`tools/schedule_task.py` — 单一工具，`action` 参数区分 create / list / cancel。
**不走自动发现**（在 `tools/__init__.py` 的 `_skip_modules` 里），因为它需要注入
`ScheduledTaskService`，由 orchestrator 手动注册。

- `action="create"` — 从 `SessionContext` 读当前 `session_key` / `channel`，
  校验 cron + task，调用 `service.add_task(...)`
- `action="list"` — 列出当前 session 的任务（system 任务标"（内置）"）
- `action="cancel"` — 按 `task_id` 或 `keyword` 取消（多个匹配时要求精确 ID）

LLM 负责把"每天八点…"翻译成 cron 表达式和一句中文指令。

### Cron 表达式规则

使用 **croniter** 库，标准 5 字段 cron 表达式（空格分隔），基于**本地时区**计算下次触发时间：

```
分钟  小时  日  月  星期
 0-59  0-23  1-31  1-12  0-6 (0=周日)
```

| 字段 | 取值范围 | 特殊语法 |
|------|---------|---------|
| 分钟 | `0-59` | `*` `,` `-` `/` |
| 小时 | `0-23` | `*` `,` `-` `/` |
| 日 | `1-31` | `*` `,` `-` `/` |
| 月 | `1-12` | `*` `,` `-` `/` |
| 星期 | `0-6` (0=周日) | `*` `,` `-` `/` |

**常用语法**：

| 语法 | 含义 | 示例 |
|------|------|------|
| `*` | 任意值 | `0 8 * * *` — 每天 8:00 |
| `*/N` | 每隔 N | `*/30 * * * *` — 每 30 分钟 |
| `A,B,C` | 枚举 | `0 8,20 * * *` — 每天 8:00 和 20:00 |
| `A-B` | 范围 | `0 9 * * 1-5` — 工作日 9:00 |
| `A-B/N` | 范围内每隔 N | `0 9-17/2 * * *` — 9:00-17:00 每 2 小时 |

**典型示例**：

| 自然语言 | cron 表达式 |
|---------|------------|
| 每天早上 8 点 | `0 8 * * *` |
| 每天晚上 20 点 | `0 20 * * *` |
| 每隔 1 小时 | `0 * * * *` |
| 每隔 30 分钟 | `*/30 * * * *` |
| 工作日早上 9 点 | `0 9 * * 1-5` |
| 每周一和周四 10 点 | `0 10 * * 1,4` |
| 每月 1 号零点 | `0 0 1 * *` |
| 每 2 小时 | `0 */2 * * *` |

**注意事项**：
- 日 和 星期 同时指定时是 **OR** 关系（不是 AND），如 `0 8 1 * 1` 表示「每月 1 号 **或** 每周一」
- 最小间隔为 1 分钟，不支持秒级精度
- 时区跟随系统本地时间，不显式指定 UTC offset

### SessionContext —— 工具怎么知道当前会话

工具的 `execute(**kwargs)` 不接收 `session_key`（`runner.py` 只传 `tools.execute(name, args)`）。
用一个 `ContextVar[SessionContext]`（`core/session_context.py`）解决：`process_message`
在跑 agent 前 `set_current(SessionContext(session_key, source))`，`finally` 里 `reset`。
`schedule_task` 工具 `get_current()` 即可拿到会话——镜像 `observability/trace.py` 的
tracer contextvar 模式，避免改动每个工具的签名。

## deliver 回调 —— 低耦合缝

`ScheduledTaskService` 从不 import 任何渠道（WeChat / HTTP）。每个入口点绑定自己的
`MessageBus`，通过 `set_deliver(cb)` 注入投递逻辑：

- **`channels/wechat.py`** — `_deliver_scheduled(task)` 把 prompt 作为 `InboundMessage`
  注入 `bus.inbound(session_key)`，复用整条 serve → outbound("wechat") → consumer 管道，
  结果推送到该 session 最近出现的聊天窗口
- **`core/server.py`** — `_deliver_scheduled(task)` 用 `source="push:{session_key}"` 注入，
  serve() 因此把该轮所有 outbound 打到**按会话专属**的 `push:{session_key}` 通道；浏览器通过
  长连接 `GET /events/{session_id}` SSE 端点持续消费这个通道，收到即渲染成 assistant 气泡

> **两条推送路径的区别**：`/chat/{sid}`（POST）是**请求作用域**——按 correlation_id 过滤、
> `final` 后关流，只服务当次对话；`/events/{sid}`（GET）是**会话作用域**的常驻 SSE——消费
> `push:{sid}` 通道、不按 correlation_id 过滤、每 5s 一次 keep-alive 保活
> （`asyncio.wait_for(queue.get(), timeout=5.0)` 超时即发 `: keepalive`）；只在浏览器标签页
> 断连时退出——每轮循环先 `await request.is_disconnected()`，返回 True 就 break，避免每次页面
> 加载泄漏一个空闲生成器。专门接收定时任务推送，定时结果走 `push:` 通道，绝不与用户当次聊天的
> `http` 通道混淆。
>
> **已知限制**：`push:{sid}` 是单个 `asyncio.Queue`，同一 session 开多个 `/events` 标签页会
> **竞争消费**（每条消息只有一个标签页收到）；v1 单用户场景可接受，未做 fan-out。
> WeChat 的 `_consume_outbound` 常驻，本就能收到推送。

## Orchestrator 装配

`Orchestrator.__init__`（`core/orchestrator.py`）：

```python
self.cron.register_job("dream", interval_hours=2)          # dream 保持原生

self._scheduled = ScheduledTaskService(
    self.workspace, self.cron,
    run_agent=self._run_scheduled_agent,                   # 内部副作用回调
)
self._scheduled.seed_system_task(                          # 小红书系统任务
    task_id="xiaohongshu", schedule="0 20 * * *",
    prompt=xiaohongshu_prompt(self.workspace),
    session_key=XIAOHONGSHU_SESSION_KEY, skills=["xiaohongshu"],
)
self._scheduled.load()                                     # 恢复用户任务
self._tools.register(ScheduleTaskTool(self._scheduled))    # 手动注册工具
```

`_on_cron_job` 折叠成两支：`dream` 走原生 `dream.run()`，其余全部
`await self._scheduled.fire(name)`。`deliver` 回调由入口点在
`start_services()` 之前用 `scheduled_tasks.set_deliver(...)` 注入。

## fire 路由

```python
async def fire(self, job_name: str) -> None:
    _, _, task_id = job_name.partition(":")     # "user:a1b2" → "a1b2"
    task = self._tasks.get(task_id)
    if task.channel:                            # push 任务
        await self._deliver(task)
    else:                                       # 内部副作用任务
        await self._run_agent(task.session_key, task.prompt, task.skills)
```

## 端到端流程（user push 任务）

```
用户: "每天八点给我推送智能体前沿进展"
  → agent 调用 schedule_task(action="create", cron="0 8 * * *", task="搜索并总结…")
    → get_current() 拿到 (session_key, channel="wechat")
    → service.add_task(...) → 注册 cron job "user:a1b2" + 持久化
  ← "✅ 已创建定时任务 a1b2"

（每天 08:00）
CronScheduler._on_timer → _on_cron_job("user:a1b2")
  → ScheduledTaskService.fire("user:a1b2")
    → deliver 回调（WeChat）: 注入 InboundMessage(prompt) 到 bus
      → serve() → process_message → agent 执行 → OutboundMessage("final")
        → _consume_outbound → itchat.send → 推送到用户微信
```

## 设计要点

- **cron 用 croniter** — LLM 可靠地输出 cron 字符串，一个字符串覆盖每天/每周/间隔/工作日；
  `croniter(expr, base).get_next(datetime)` 给出本地时区下次运行时刻，无需手写日历数学
- **`channel=None` = 副作用任务** — 无用户可推送时（小红书）走 `run_agent` 直跑，丢弃输出
- **ContextVar 注入会话** — 工具不改签名即可拿到当前 session
- **注入 deliver 回调** — service 不 import 渠道，每个入口点绑定自己的 bus，这是低耦合缝
- **`system=True`** — 保护内置任务不被用户 `cancel`，`list` 仍只读可见
- **注册顺序** — 先 `register_job`（无效 cron 立刻 `ValueError`）再持久化，避免写入坏任务
- **Dream 不变** — dream 走原生方法而非 agent prompt，保持独立的 cron job

## 相关文档

- [Dream 定时调度机制](dream-scheduler.md) — 底层 `CronScheduler` 自驱动定时器
- [小红书海龟汤自动运营](xiaohongshu-turtle-soup.md) — 系统任务的完整案例
- [流式输出与路由](streaming-and-routing.md) — MessageBus / serve / outbound 管道
