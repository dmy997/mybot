# Dream 定时调度机制 (Dream Scheduler)

## 概述

Dream 是 mybot 记忆系统的**周期性 LLM 记忆合并模块**，由 `CronScheduler` 每 2 小时自动触发。采用 nanobot `_arm_timer` **自驱动定时器模式**——不依赖外部 `tick()` 调用或用户输入，后台 `asyncio.Task` 自循环。

## 架构概览

```
Orchestrator.__init__
  ├── Dream(store, provider, model)
  └── CronScheduler(state_dir, on_job=_on_cron_job)
        └── register_job("dream", interval_hours=2)

Orchestrator.start_services()
  └── cron.start() → _arm_timer() → asyncio.create_task(tick())
                                      └── sleep(delay) → _on_timer()
                                                          ├── _execute_job("dream")
                                                          │     └── on_job("dream") → _on_cron_job("dream")
                                                          │           └── dream.run()
                                                          └── _arm_timer()  ← re-arm, loop continues
```

## CronScheduler — 自驱动定时器

`services/cron.py`

### _arm_timer 模式

核心思想：每次唤醒后**自己重新安排下一次唤醒**，形成闭环。

```
_arm_timer()
  ├── 计算 next_wake = min(所有 job 的 next_run_at_ms)
  ├── delay = min(300s, max(0, next_wake - now))
  └── asyncio.create_task(tick())
        └── await asyncio.sleep(delay)
        └── if still running: await _on_timer()

_on_timer()
  ├── 遍历所有 job，触发已到期的
  │     └── _execute_job(job)
  └── _arm_timer()  ← 重新安排，形成闭环
```

关键常量：`_MAX_SLEEP_MS = 300_000`（5 分钟）——即使无待处理 job 也会定期唤醒，检查是否有新注册的 job。

### CronJob 数据结构

```python
@dataclass
class CronJob:
    name: str
    interval_hours: float
    next_run_at_ms: int = 0    # 下次运行时间戳（毫秒）
    last_run_at_ms: int = 0    # 上次运行时间戳
    last_status: str | None    # "ok" | "error"
    last_error: str | None
```

### 首次运行延迟

新注册的 job 不会立即运行，而是延迟完整的 `interval_hours`：

```python
if job.last_run_at_ms > 0:
    job.next_run_at_ms = job.last_run_at_ms + int(interval_hours * 3600 * 1000)
else:
    # 首次运行：从现在起延迟完整的间隔
    job.next_run_at_ms = _now_ms() + int(interval_hours * 3600 * 1000)
```

### 状态持久化

`cron_state.json` 记录每个 job 的 `last_run_at_ms`、`last_status`、`last_error`，服务重启后恢复：

```python
def _load_state(self) -> dict:
    # 读取 cron_state.json，恢复各 job 的上次运行时间

def _save_state(self) -> None:
    # 原子写入（tmp + replace）cron_state.json
```

### 并发保护

每个 job 有独立的 `asyncio.Lock`，防止同一 job 并发执行：

```python
lock = self._locks.setdefault(job.name, asyncio.Lock())
if lock.locked():
    return  # 上一轮还在运行，跳过本次
async with lock:
    ...
```

### 失败重试

job 执行失败时立即重试一次（共 2 次 attempt），之后标记为 error 并安排下次运行：

```python
for attempt in (1, 2):
    try:
        if self.on_job:
            await self.on_job(job.name)
        ok = True
        break
    except Exception as exc:
        if attempt == 1:
            logger.warning("Cron: job {!r} failed (attempt 1), retrying once")
```

## Dream — 周期记忆合并

`memory/dream.py`

### 数据结构

Dream 管理三个游标/日期文件：

| 文件 | 管理者 | 用途 |
|------|--------|------|
| `.cursor` | Consolidator | 写入游标（单调递增 int） |
| `.dream_cursor` | Dream | 消费游标——Dream 已处理到的位置 |
| `.dream_date` | Dream | Dream 上次运行日期（用于行龄注释 `<- Nd`） |

### Dream 生命周期（7 步）

```python
async def run(self) -> bool:
    # 1. 检查 provider 是否配置
    if self.provider is None: return False

    # 2. 读取增量历史摘要（dream_cursor 之后的新条目）
    dream_cursor = self.store.get_dream_cursor()
    new_entries = self.store.read_history(since_cursor=dream_cursor)
    if not new_entries: return False

    # 批量上限：最多处理 20 条
    if len(new_entries) > 20:
        new_entries = new_entries[-20:]

    # 3. 读取当前三份记忆文件
    soul = self.store.read_soul()
    user = self.store.read_user()
    current_memory = self.store.read_memory_file()

    # 4. 更新 MEMORY.md 行龄注释（如果日期变更），立即写回磁盘
    today = date.today().isoformat()
    last_date = self.store.get_dream_date()
    if last_date and last_date != today:
        updated_memory = self._update_age_annotations(current_memory, last_date, today)
        if updated_memory is not None:
            current_memory = updated_memory
            self.store.write_memory_file(current_memory)

    # 5. Phase 1 — LLM 分析 → 结构化指令
    directives = await self._call_llm(soul, user, current_memory, new_entries)

    # 6. Phase 2 — 解析指令，程序化合并到各文件
    adds, removes = self._parse_directives(directives)
    changed = False
    changed |= self._apply_adds(adds)      # 追加 + 去重
    changed |= self._apply_removes(removes) # 精确/多行块匹配删除

    # 7. 推进游标 + 记录日期
    self.store.set_dream_date(today)
    if changed:
        self._advance_cursor(new_entries)
    return changed
```

### Phase 1 — LLM 分析

调用 LLM（使用 `dream_phase1.md` 作为 system prompt，`dream_user.md` 作为 user message 模板），输入：SOUL.md、USER.md、MEMORY.md、新历史摘要条目。

输出格式：

```
[FILE] SOUL.md: 用中文回答除非明确要求其他语言
[FILE] USER.md: 主要语言是中文
[FILE-REMOVE] USER.md: - **Language**: English
[FILE] MEMORY.md: 项目使用 PostgreSQL 生产环境，SQLite 测试
[SKIP]
```

### Phase 2 — 程序化合并

| 指令 | 行为 |
|------|------|
| `[FILE] SOUL.md: ...` | 追加到 SOUL.md（去重：大小写不敏感子串匹配） |
| `[FILE] USER.md: ...` | 追加到 USER.md |
| `[FILE] MEMORY.md: ...` | 追加到 MEMORY.md，带 `<- 0d` 行龄注释 |
| `[FILE-REMOVE] ...` | 从目标文件删除匹配内容 |
| `[SKIP]` | 无变更 |

REMOVE 匹配顺序：精确匹配 → 多行块匹配（所有行按序出现则删除整个块）。

修正模式：同一轮同时出现 `[FILE]` + `[FILE-REMOVE]` 实现新旧替换。例如添加"住在上海"同时删除"住在北京"。

### 行龄注释

MEMORY.md 中每条记忆带 `<- Nd` 注释表示距今天数。Dream 运行时若检测到日期变更，自动递增所有行龄：

```python
# "住在上海  <- 3d" → "住在上海  <- 4d"
def _update_age_annotations(text, last_date, today):
    delta = (today - last_date).days
    if delta > 0:
        new_text = re.sub(r"  <- (\d+)d", lambda m: f"  <- {int(m.group(1)) + delta}d", text)
```

## 完整调用链

### 启动时

```
Orchestrator.__init__()
  ├── self._dream = Dream(store, provider, model)
  ├── self.cron = CronScheduler(state_dir, on_job=self._on_cron_job)
  └── self.cron.register_job("dream", interval_hours=2)
        ├── 创建 CronJob(name="dream", interval_hours=2)
        ├── 从 cron_state.json 恢复 last_run_at_ms（若有）
        ├── 计算 next_run_at_ms（首次 = now + 2h，后续 = last + 2h）
        └── _ensure_loop() → _arm_timer()

Orchestrator.start_services()
  └── self.cron.start()
        ├── self._running = True
        └── self._arm_timer()
              └── asyncio.create_task(tick())
```

### 定时器触发

```
tick()  ← asyncio.Task
  └── await asyncio.sleep(delay)  # delay = min(300s, next_wake - now)
  └── if self._running:
        await _on_timer()
          ├── 找出所有 next_run_at_ms <= now 的 job
          ├── for job in due:
          │     await _execute_job(job)
          │       ├── lock.acquire()（per-job 去重）
          │       ├── await self.on_job("dream")  ← 即 _on_cron_job
          │       │     └── await self._dream.run()
          │       │           ├── 读取 dream_cursor
          │       │           ├── 读取 history.jsonl 新条目
          │       │           ├── 读取 SOUL/USER/MEMORY.md
          │       │           ├── 更新行龄注释
          │       │           ├── Phase 1: LLM 分析
          │       │           ├── Phase 2: 程序化合并
          │       │           └── 推进 dream_cursor + 记录 dream_date
          │       ├── 更新 job.last_run_at_ms, next_run_at_ms
          │       └── lock.release()
          └── _arm_timer()  ← 重新安排下次唤醒
```

### 手动触发

```python
await cron.run_job_now("dream")
  └── await _execute_job(job)
        └── ... (同上)
```

### 关闭

```
Orchestrator.stop_services()
  └── self.cron.stop()
        ├── self._running = False
        └── self._timer_task.cancel()  # 取消当前的 asyncio.sleep
```

## Consolidator vs Dream 对比

| 维度 | Consolidator（实时） | Dream（周期） |
|------|---------------------|--------------|
| **触发时机** | 每轮对话后（fire-and-forget） | 每 2 小时（CronScheduler） |
| **输入** | 本轮对话消息 | history.jsonl 新条目 |
| **输出** | history.jsonl（追加一行 JSON） | SOUL.md / USER.md / MEMORY.md |
| **延迟** | 秒级（异步不阻塞） | 最长 2 小时 |
| **LLM 调用** | 摘要生成 | 结构化指令生成 |
| **写入方式** | 追加（append + fsync） | 原子覆盖（tmp + os.replace） |
| **游标** | 写入 `.cursor` | 消费 `.dream_cursor` |

## 设计要点

- **自驱动闭环**：`_arm_timer() → sleep → _on_timer() → _arm_timer()`，不依赖任何外部 tick
- **5 分钟唤醒上限**：即使无 job 到期也会定期唤醒，确保新注册的 job 能被及时调度
- **首次延迟完整间隔**：新 job 不会立即运行，避免用户刚启动就被 Dream 打扰
- **Per-job 去重锁**：同一 job 不会并发执行，上一轮未完成则跳过本次
- **失败重试一次**：job 执行失败立即重试，之后标记错误并安排下次运行
- **状态持久化**：`cron_state.json` 记录最后运行时间，重启后按间隔继续（不会因重启而重置计时）
- **原子写入**：cron 状态和 memory 文件均使用 `tmp + replace` 模式
- **Phase 1/2 分离**：LLM 产出结构化指令 → 程序化精确合并，避免 LLM 直接修改文件内容的风险
