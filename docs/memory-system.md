# 记忆系统 (Memory System)

## 概述

mybot 的记忆系统管理 **四类随时间演化的文件**，采用 **两阶段 LLM 驱动架构**：实时 Consolidator（对话摘要 → `history.jsonl`）+ 周期性 Dream（摘要合并 → `MEMORY.md`）。

```
记忆系统管理的文件                非记忆文件（静态指令/任务清单）
═══════════════════              ═══════════════════════════════
workspace/                       prompt_templates/
├── SOUL.md  ← bot 身份/人格     ├── AGENTS.md    ← 工作区惯例、工具使用规则
├── USER.md  ← 用户画像          └── HEARTBEAT.md ← 周期性任务清单
└── memory/
    ├── MEMORY.md   ← 长期记忆
    └── history.jsonl ← 对话摘要
```

**判断标准**：记忆文件随对话积累而演化（Consolidator/Dream/用户编辑会修改它们）；非记忆文件是静态指令或清单，不随对话自动更新。

参考：nanobot `agent/memory.py` Consolidator + Dream 架构。

## 目录布局

```
workspace/
├── SOUL.md                          # AI 助手的自我描述（bot 身份/行为准则）
├── USER.md                          # 用户画像（偏好、基本信息、技术栈）
├── memory/
│   ├── MEMORY.md                    # 长期记忆（Dream 维护 + 手动 remember）
│   ├── history.jsonl                # 追加式对话摘要（Consolidator 写入，Dream 读取）
│   ├── .cursor                      # Consolidator 写入游标（单调递增 int）
│   ├── .dream_cursor                # Dream 消费游标（单调递增 int）
│   └── .dream_date                  # Dream 上次运行日期（用于行龄注释）
├── cron/
│   └── cron_state.json              # Cron 调度器状态（Dream 上次运行时间）
└── sessions/                        # 会话 JSON 文件
```

### 与静态指令文件的区别

| 文件 | 位置 | 管理者 | 随对话演化？ | 注入提示词？ |
|------|------|--------|-------------|-------------|
| **SOUL.md** | workspace/ | MemoryStore / 用户编辑 | 是 | 始终注入 |
| **USER.md** | workspace/ | MemoryStore / 用户编辑 | 是 | 始终注入 |
| **MEMORY.md** | memory/ | Dream（LLM 合并） | 是 | 始终注入 |
| **history.jsonl** | memory/ | Consolidator（LLM 摘要） | 是 | Dream 未处理的条目注入 |
| AGENTS.md | prompt_templates/ | 静态模板 | 否 | 否（是 agent 指令的一部分） |
| HEARTBEAT.md | prompt_templates/ | 用户编辑 | 手动更新 | 否（是 heartbeat service 的任务源） |

**AGENTS.md** 和 **HEARTBEAT.md** 不是记忆文件。它们提供静态项目配置和任务清单，不存储从对话中提取的事实，也不受 Consolidator/Dream 管理。

## 核心架构

记忆系统管理三份核心文件 + 一份中间存储，全部由 Dream 周期维护：

```
                    ┌─────────────────────────────────┐
                    │         MemoryStore              │
                    │    (统一文件 I/O 底层)            │
                    └──────────┬──────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────────┐
        │                      │                          │
   ┌────▼─────┐         ┌──────▼──────┐          ┌───────▼──────┐
   │ SOUL.md  │         │  USER.md    │          │  MEMORY.md   │
   │ bot 身份  │         │  用户画像    │          │  长期记忆     │
   └────▲─────┘         └──────▲──────┘          └───────▲──────┘
        │                      │                          │
        └──────────────────────┼──────────────────────────┘
                               │
                     Dream 写入三份文件
                               │
              ┌────────────────┴────────────────┐
              │                                 │
              │       history.jsonl              │  ← Consolidator 写入
              │    (追加式对话摘要)                │
              └────────────────▲────────────────┘
                               │
                    用户对话 → Consolidator
                    (每轮 fire-and-forget)
```

**写入策略（nanobot 模式）**：

```
对话 → Consolidator → history.jsonl → Dream → SOUL.md   (行为变更)
                                            → USER.md   (用户事实)
                                            → MEMORY.md (知识/决策)
```

| 文件 | 管理方式 | Dream 何时写入？ |
|------|---------|----------------|
| `SOUL.md` | Dream 自动维护 + 用户可手动编辑 | 用户明确请求行为变更时 |
| `USER.md` | Dream 自动维护 + 用户可手动编辑 | 发现新用户事实/偏好/修正时 |
| `MEMORY.md` | Dream 自动维护 | 发现新知识/决策/项目上下文时 |
| `history.jsonl` | Consolidator 写入 | 每次对话后（token 预算触发） |

Dream Phase 1（LLM 分析）产出 `[FILE]` 和 `[FILE-REMOVE]` 指令，Phase 2（程序化合并）分别应用到三个文件。同一轮可以同时新增（`[FILE]`）和删除（`[FILE-REMOVE]`），实现修正模式：`[FILE] USER.md: 住在上海` + `[FILE-REMOVE] USER.md: 住在北京`。

## 模块清单

| 模块 | 文件 | 职责 |
|------|------|------|
| MemoryStore | `memory/store.py` | 所有文件 I/O：SOUL.md, USER.md, MEMORY.md, history.jsonl, cursor 管理 |
| **Consolidator** | `memory/consolidator.py` | 实时 token 预算驱动的对话摘要（异步 fire-and-forget） |
| **Dream** | `memory/dream.py` | 周期性 LLM 记忆合并（由 CronScheduler 触发） |
| **CronScheduler** | `services/cron.py` | 通用自驱动定时调度器（nanobot `_arm_timer` 模式） |

## 1. MemoryStore — 纯文件 I/O

`memory/store.py`

统一的文件读写底层，不包含业务逻辑。

### 核心文件操作

```python
# -- SOUL.md / USER.md --
store.read_soul()           → str
store.write_soul(content)

# -- MEMORY.md（长期记忆，Dream 维护）--
store.read_memory_file()    → str
store.write_memory_file(content)  # 原子写入（tmp + replace）

# -- history.jsonl（对话摘要，Consolidator 写入）--
store.append_history(entry, *, max_chars=None, session_key="") → int  # 返回单调递增 cursor
store.read_history(since_cursor=0) → list[dict]
store.raw_archive(messages, session_key="")         # LLM 失败时的降级路径

# -- cursor 管理 --
store.get_cursor()          → int      # Consolidator 写入游标
store.get_dream_cursor()    → int      # Dream 消费游标
store.set_dream_cursor(cursor)
```

### 原子写入

所有关键写入（`write_memory_file`, `append_history`, `_write_entries`）采用 `tmp + os.fsync + os.replace` 模式，确保崩溃或 SIGKILL 不会损坏文件。

## 2. Consolidator — 实时对话摘要

`memory/consolidator.py`

每次对话轮次后异步触发（`asyncio.create_task`），不阻塞用户交互。

```python
class Consolidator:
    def __init__(self, store, provider=None, model="", *,
                 context_window_tokens=128_000, consolidation_ratio=0.7): ...

    async def maybe_consolidate(self, session, build_messages_fn=None) -> bool:
        """检查 token 预算，超出阈值则压缩."""
        # 1. 统计未归档消息
        # 2. 估计 token 数
        # 3. 若超过 budget: 分轮次 LLM 摘要 → history.jsonl
        # 4. 推进 session.last_consolidated

    async def archive(self, messages, session_key="") -> str | None:
        """调用 LLM 摘要消息，写入 history.jsonl."""
```

- **Token 估计**: ~4 chars ≈ 1 token
- **边界选择**: 对齐 user turn 边界，不切断 tool_call/tool_result 对
- **每 session 锁**: `asyncio.Lock` 防止同一 session 并发压缩
- **降级路径**: LLM 调用失败时 `raw_archive()` 直接保存原始文本

## 3. Dream — 周期记忆合并（两阶段）

`memory/dream.py`

每 2 小时由 CronScheduler 触发一次。采用 nanobot 两阶段架构：

**Phase 1 — LLM 分析**：读取 SOUL.md、USER.md、MEMORY.md + history.jsonl 新条目 → LLM 产出结构化指令。

**Phase 2 — 程序化合并**：解析指令，分别应用到三个文件。

```python
class Dream:
    def __init__(self, store, provider=None, model=""): ...

    async def run() -> bool:
        """执行一轮 Dream 周期."""
        # 1. 读取 history.jsonl（since .dream_cursor）
        # 2. 读取 SOUL.md, USER.md, MEMORY.md
        # 3. Phase 1: LLM → [FILE] / [FILE-REMOVE] 指令
        # 4. Phase 2: 解析指令，程序化应用到各文件
        # 5. 原子写入变更的文件
        # 6. 推进 .dream_cursor
```

### Phase 1 输出格式

```
[FILE] SOUL.md: 用中文回答除非明确要求其他语言
[FILE] USER.md: 主要语言是中文
[FILE-REMOVE] USER.md: - **Language**: English
[FILE] MEMORY.md: 项目使用 PostgreSQL 生产环境，SQLite 测试
[FILE-REMOVE] MEMORY.md: 数据库方案未定
[SKIP]
```

### Phase 2 合并规则

| 指令 | 行为 |
|------|------|
| `[FILE] SOUL.md: ...` | 追加事实到 SOUL.md |
| `[FILE] USER.md: ...` | 追加事实到 USER.md |
| `[FILE] MEMORY.md: ...` | 追加事实到 MEMORY.md |
| `[FILE-REMOVE] SOUL.md: ...` | 从 SOUL.md 中删除匹配内容 |
| `[FILE-REMOVE] USER.md: ...` | 从 USER.md 中删除匹配内容 |
| `[FILE-REMOVE] MEMORY.md: ...` | 从 MEMORY.md 中删除匹配内容 |
| `[SKIP]` | 无变更 |

REMOVE 匹配顺序：精确匹配 → 多行块匹配。未找到时记录调试日志，不中断流程。

- **批量上限**: 每次最多处理 20 条历史摘要
- **修正模式**: `[FILE]` + `[FILE-REMOVE]` 同时出现实现新旧替换
- **原子写入**: 每个文件独立原子写入（tmp + os.replace），单个文件损坏不影响其他

## 4. CronScheduler — 自驱动定时调度

`services/cron.py`

采用 nanobot `_arm_timer` 模式的通用定时调度器。不依赖用户输入——后台 `asyncio.create_task(tick())` 自循环。

```python
class CronScheduler:
    def __init__(self, state_dir, on_job=None): ...
    def register_job(name, *, interval_hours) -> CronJob: ...
    async def start() -> None: ...    # 启动 timer loop
    def stop() -> None: ...           # 取消 timer task
    async def run_job_now(name) -> bool: ...  # 手动触发
```

- **自闭环**: `_arm_timer() → sleep(delay) → _on_timer() → _arm_timer()`
- **5 分钟上限**: 即使无待处理 job 也会定期唤醒
- **per-job 锁**: `asyncio.Lock` 防止并发执行同名 job
- **状态持久化**: `cron_state.json` 记录上次运行时间，重启后按间隔继续

## 5. 与上下文系统的集成

### 系统提示词注入

`ContextManager._build_memory_context()` 直接从 MemoryStore 读取三份文件并组装为系统提示词：

```python
def _build_memory_context(self) -> str:
    parts = []
    soul = self.store.read_soul()        # SOUL.md — bot 身份
    user = self.store.read_user()        # USER.md — 用户画像
    memory = self.store.get_memory_context()  # MEMORY.md — 长期记忆
    # 组装为 "Identity / User Profile / Long-term Memory" 三段
```

`ContextManager._build_history_context()` 将 Dream 尚未处理的 `history.jsonl` 条目注入到系统提示词中，确保新近对话在下次 Dream 运行前就对 LLM 可见：

```python
def _build_history_context(self, max_entries=20, max_chars=16_000) -> str:
    dream_cursor = self.store.get_dream_cursor()
    entries = self.store.read_history(since_cursor=dream_cursor)
    # 格式化最近的摘要条目，注入提示词
```

## 设计要点

- **LLM 替代关键词**: 用 LLM 摘要替代关键词匹配，提取的事实更准确，自然去重
- **两级时效性**: Consolidator（秒级延迟）+ Dream（2 小时合并），兼顾实时性和质量
- **异步不阻塞**: Consolidator fire-and-forget，Dream 后台执行，用户交互不受影响
- **自驱动 cron**: 后台 timer loop 不依赖用户输入，闲置时照样运行
- **原子写入**: 所有文件操作使用 tmp+fsync+replace，崩溃安全
