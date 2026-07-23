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

记忆系统采用 **双层架构**：内置文件记忆层（始终活跃）+ 可插拔外部记忆提供者（最多一个）。

```
                         ┌──────────────────────────┐
                         │     MemoryManager         │
                         │  故障隔离 + 工具路由        │
                         │  内置(builtin) + 外部插件   │
                         └─────┬──────────┬──────────┘
                               │          │
                    ┌──────────┘          └──────────────┐
                    ▼                                    ▼
          ┌─────────────────┐                ┌──────────────────┐
          │ BuiltinProvider  │                │ ExternalProvider  │
          │ (包装现有系统)     │                │ (可选, 最多 1 个)  │
          └────────┬────────┘                └──────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────────────────────────┐
    │              MemoryService + MemoryStore              │
    │         (文件 I/O + 混合搜索 + 冻结快照)               │
    └──────────────────────────────────────────────────────┘
```

底层文件存储仍由 Dream 周期维护：

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
| **MemoryProvider** | `memory/provider.py` | 可插拔记忆后端的抽象基类 (ABC)，定义 `system_prompt_block`, `prefetch`, `sync_turn`, `handle_tool_call` 等生命周期钩子 |
| **BuiltinMemoryProvider** | `memory/builtin_provider.py` | 内置记忆提供者，包装 MemoryService，提供 `memory_remember/recall/forget` 工具 schema |
| **MemoryManager** | `memory/manager.py` | 协调内置 + 外部提供者，故障隔离，工具名路由，最多一个外部提供者 |
| **Consolidator** | `memory/consolidator.py` | 实时 token 预算驱动的对话摘要（异步 fire-and-forget） |
| **Dream** | `memory/dream.py` | 周期性 LLM 记忆合并（由 CronScheduler 触发） |
| **CronScheduler** | `services/cron.py` | 通用自驱动定时调度器（nanobot `_arm_timer` 模式） |
| **Provider Discovery** | `plugins/memory/__init__.py` | 扫描 `plugins/memory/` 目录发现外部 MemoryProvider 实现 |
| **Context Scrubbing** | `memory/scrubber.py` | `<memory-context>` 标签清洗 + 流式输出状态机 |

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
                 context_window_tokens=200_000, consolidation_ratio=0.7): ...

    async def maybe_consolidate(self, session, build_messages_fn=None) -> bool:
        """检查 token 预算，超出阈值则压缩."""
        # 1. 统计未归档消息
        # 2. 估计 token 数
        # 3. 若超过 budget: 分轮次 LLM 摘要 → history.jsonl
        # 4. 推进 session.last_consolidated

    async def archive(self, messages, session_key="",
                      instructions=None) -> str | None:
        """调用 LLM 摘要消息，写入 history.jsonl.
        当提供 instructions 时，会注入到系统提示词中指导摘要重点。"""
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

## 5. 双层记忆架构 (Dual-Layer Memory)

### 5.1 MemoryProvider — 可插拔抽象基类

`memory/provider.py`

所有记忆后端（内置或外部）须实现的 ABC。所有 IO 能力方法均为 `async`。

```python
class MemoryProvider(ABC):
    # -- 必须实现 --
    name: str                                      # 标识符, e.g. "builtin", "honcho"
    def is_available() -> bool: ...                # 同步检查依赖/配置，不可联网
    async def initialize(session_id, **kwargs): ... # 连接/创建资源
    def get_tool_schemas() -> list[dict]: ...      # OpenAI function-calling schemas

    # -- 可选（有默认 no-op）--
    async def system_prompt_block() -> str: ...    # 静态指令注入
    async def prefetch(query, *, session_id) -> str: ...  # 每轮动态召回
    async def queue_prefetch(query, *, session_id): ...   # 后台预取（下一轮预热）
    async def sync_turn(user, assistant, *, session_id): ...  # 持久化对话轮次
    async def handle_tool_call(name, args) -> Any: ...  # 分发工具调用
    async def on_session_end(messages): ...              # 会话结束钩子
    async def on_memory_write(action, target, content, metadata): ...  # 镜像内置写入
```

### 5.2 BuiltinMemoryProvider — 内置提供者

`memory/builtin_provider.py`

始终活跃的提供者，包装现有的 `MemoryService`：

- `name = "builtin"`，`is_available()` 永远返回 `True`
- 提供 3 个工具 schema：`memory_remember`, `memory_recall`, `memory_forget`
- `system_prompt_block()` 和 `prefetch()` 返回空字符串——内置记忆内容通过冻结快照路径注入
- `handle_tool_call()` 路由到 `MemoryService.remember()/forget()/recall()`

### 5.3 MemoryManager — 协调器

`memory/manager.py`

协调内置 + 可选外部提供者，关键行为：

- **注册**: 内置始终接受；最多一个外部提供者（第二个被拒绝并警告）
- **故障隔离**: 每个 `try/except` 包装所有提供者调用，一个失败不阻塞其他
- **工具路由**: `tool_name → provider` 索引，首次注册优先
- **Schema 去重**: `get_all_tool_schemas()` 跨提供者去重
- **生命周期**: `initialize_all()` → `prefetch_all()` → `sync_all()` → `shutdown_all()`
- **会话钩子**: `on_session_end()` 和 `on_memory_write()` 仅转发给外部提供者

### 5.4 提供者发现

`plugins/memory/__init__.py`

`discover_providers(workspace)` 扫描两个位置：
1. 捆绑的 `plugins/memory/` 目录
2. 工作区 `workspace/plugins/memory/` 目录

发现机制：导入目录下的 Python 包 → 查找 `MemoryProvider` 子类 → 实例化 → 检查 `is_available()`。

### 5.5 冻结快照 (Frozen Snapshot)

`context/memory_service.py` — `MemoryService.build_memory_context()`

首次调用（per session_key）读取 SOUL.md + USER.md + MEMORY.md 并缓存为"冻结快照"。同一会话的后续调用返回缓存的快照不变。

```python
# 首次调用: 读取磁盘 → 缓存快照
snapshot = memory.build_memory_context(session_key="sess-1")
# 后续调用: 返回缓存（保持 LLM prefix cache 命中）
snapshot = memory.build_memory_context(session_key="sess-1")

# 会话切换/重置时失效
memory.invalidate_snapshot("sess-1")
```

**设计意图**: 保持系统提示词前缀稳定，使 LLM 的 prefix caching 在整个会话中持续命中，降低成本。

### 5.6 上下文围栏 (Context Fencing)

`memory/scrubber.py`

外部记忆提供者的内容包裹在 `<memory-context>` 标签中注入系统提示词，由 `build_memory_context_block()` 构建围栏块：

```python
build_memory_context_block("外部记忆内容")
# → "[System note: ...]\n<memory-context>\n外部记忆内容\n</memory-context>"
```

`StreamingContextScrubber` 是有状态流式输出清洗器，处理跨块分割的标签：
- `feed(chunk)` — 输入流式块，返回可见（非清洗）部分
- `flush()` — 流结束时排出持有的缓冲区
- 如果流在不匹配的 `<memory-context>` 中结束，丢弃内容（视为未闭合围栏）

## 6. 与上下文系统的集成

### 系统提示词组装

`ContextManager._build_system_prompt()` 按以下顺序组装记忆内容：

```
1. MemoryManager.build_system_prompt()     ← 外部提供者的静态指令
2. MemoryService.build_memory_context()    ← 冻结快照 (SOUL + USER + MEMORY)
     └─ 包裹在 <memory-context> 围栏中
3. MemoryManager.prefetch_all(query)       ← 外部提供者的动态召回
     └─ 包裹在 <memory-context> 围栏中
4. _build_history_context()               ← Dream 未处理的 history.jsonl 条目
```

### 冻结快照注入

`MemoryService.build_memory_context(session_key, query)` — 首次调用从磁盘读取三份文件并缓存；后续调用返回缓存不变：

```python
def build_memory_context(self, session_key: str = "", query: str | None = None) -> str:
    if session_key and session_key in self._snapshots:
        return self._snapshots[session_key][0]  # 缓存命中
    # 首次: 读取 SOUL.md + USER.md + MEMORY.md → 缓存 → 返回
    context = self._build_fresh(query)
    if session_key:
        self._snapshots[session_key] = (context, time.monotonic())
    return context
```

`ContextManager._build_history_context()` 将 Dream 尚未处理的 `history.jsonl` 条目注入到系统提示词中，确保新近对话在下次 Dream 运行前就对 LLM 可见：

```python
def _build_history_context(self, max_entries=20, max_chars=16_000) -> str:
    dream_cursor = self.store.get_dream_cursor()
    entries = self.store.read_history(since_cursor=dream_cursor)
    # 格式化最近的摘要条目，注入提示词
```

## 7. 代码调用链

### 7.1 系统启动：记忆系统初始化

```
Orchestrator.__init__()                               # core/orchestrator.py:92
  ├─ MemoryStore(workspace)                           # memory/store.py:28
  │   ├─ _ensure_dir("memory/")
  │   └─ _ensure_dir("cron/")
  ├─ ctx = ContextManager(workspace, store, provider)
  ├─ MemoryManager()                                  # memory/manager.py
  │   ├─ add_provider(BuiltinMemoryProvider(ctx.memory))
  │   └─ discover_providers(workspace)                # plugins/memory/__init__.py
  │       └─ 扫描 plugins/memory/ → MemoryProvider 子类
  ├─ ctx._memory_manager = memory_manager              # 注入到 ContextManager
  ├─ Consolidator(store, provider, model)             # memory/consolidator.py:37
  ├─ Dream(store, provider, model)                    # memory/dream.py:41
  ├─ cron = CronScheduler(state_dir, on_job=...)      # services/cron.py:58
  │   └─ register_job("dream", interval_hours=2)      # services/cron.py:84
  └─ await cron.start()                               # services/cron.py:122
      └─ _arm_timer() → sleep → _on_timer() loop      # services/cron.py:168→195
```

### 7.2 对话后 Consolidation（实时，fire-and-forget）

```
Orchestrator.process_message()                        # core/orchestrator.py:267
  ├─ ContextManager.build_messages()                  # context/context_manager.py:295
  │   ├─ _build_memory_context() → store.read_soul/read_user/get_memory_context
  │   └─ _build_history_context() → store.read_history(since_cursor=dream_cursor)
  ├─ Dispatcher.resolve() → Agent.run() → AgentCore.run()  # core/runner.py:264
  ├─ ctx.save_exchange() → SessionManager.add_messages_to_session()
  └─ asyncio.create_task(                             # core/orchestrator.py:436
       Consolidator.maybe_consolidate(session, build_messages_fn)
     )                                                # memory/consolidator.py:80
       └─ _do_consolidate(session)                    # memory/consolidator.py:100
           ├─ 统计未归档消息 (session.messages[last_consolidated:])
           ├─ _estimate_message_tokens() → 与 token budget 比较
           ├─ 若超预算:
           │   ├─ _pick_boundary() → 对齐 user turn 边界
           │   ├─ archive(chunk)                      # memory/consolidator.py:188
           │   │   ├─ _format_messages() → LLM chat_with_retry()
           │   │   ├─ store.append_history(summary)   # memory/store.py:132
           │   │   └─ 异常降级: store.raw_archive()   # memory/store.py:182
           │   ├─ session.last_consolidated += boundary
           │   └─ 循环直到 token 在预算内
           └─ 返回 True/False
     # 若 consolidation 完成 → SessionManager.prune_archived_messages()
     #   删除 messages[:min(cursor, last_consolidated)]   # context/session.py
```

### 7.3 Dream 周期合并（每 2 小时，CronScheduler 触发）

```
CronScheduler._on_timer()                             # services/cron.py:195
  └─ Orchestrator._on_cron_job("dream")               # core/orchestrator.py:246
      └─ Dream.run()                                  # memory/dream.py:66
          ├─ 读取 SOUL.md, USER.md, MEMORY.md + history.jsonl 新条目
          │   store.read_soul() / read_user() / read_memory_file()
          │   store.read_history(since_cursor=dream_cursor)
          ├─ Phase 1: LLM 分析
          │   └─ provider.chat_with_retry() → [FILE]/[FILE-REMOVE]/[SKIP] 指令
          ├─ Phase 2: 程序化合并
          │   ├─ _parse_instructions() → 解析结构化指令
          │   ├─ _apply_adds() → 去重 + 追加到目标文件
          │   ├─ _apply_removes() → 精确匹配 → 多行块匹配删除
          │   └─ _update_age_annotations() → 行龄标记 ← Nd
          ├─ 原子写入各文件 (tmp + os.replace)
          │   store.write_soul() / write_user() / write_memory_file()
          └─ store.set_dream_cursor(new_cursor)       # 推进消费游标
```

### 7.4 上下文组装：记忆注入 LLM

```
ContextManager.build_messages(session_key, user_input) # context/context_manager.py:295
  └─ _build_system_prompt(skills, tools, file_context) # context/context_manager.py:524
      ├─ _build_static_prompt(skills, tools)           # context/context_manager.py:612
      │   ├─ 基础 system prompt 模板
      │   ├─ SkillsLoader 注入活跃 skill 的 SKILL.md
      │   └─ 工具定义注入
      ├─ MemoryManager.build_system_prompt()           # 外部提供者静态指令
      ├─ _build_memory_context(session_key, query)     # 冻结快照（首次读磁盘，后续缓存）
      │   └─ MemoryService.build_memory_context(session_key, query)
      │       ├─ session_key 有缓存 → 返回冻结快照（LLM prefix cache 命中）
      │       └─ 首次 → _build_fresh(query)
      │           ├─ store.read_soul()    → SOUL.md
      │           ├─ store.read_user()    → USER.md
      │           └─ store.get_memory_context(query) → MEMORY.md (含相关性过滤)
      │       └─ 包裹在 <memory-context> 围栏中
      ├─ MemoryManager.prefetch_all(query, session_id) # 外部提供者动态召回
      │   └─ 包裹在 <memory-context> 围栏中
      └─ _build_history_context(max_entries=20)        # 过渡层
          ├─ store.get_dream_cursor()
          ├─ store.read_history(since_cursor=dream_cursor)  # 未处理条目
          └─ 格式化为 "Recent History" 段落 (max 16K chars)
```

### 7.5 对话后同步

```
Orchestrator.process_message() 续
  └─ 保存交换后:
      ├─ MemoryManager.sync_all(user_input, assistant, session_id=session_key)
      │   └─ 外部提供者持久化对话轮次（故障隔离）
      └─ MemoryManager.queue_prefetch_all(next_input, session_id=session_key)
          └─ 外部提供者后台预取（下一轮预热）

Orchestrator.delete_session(key)
  ├─ ctx.memory.invalidate_snapshot(key)       # 清除冻结快照
  └─ memory_manager.on_session_end(messages)   # 通知外部提供者
```

### 7.6 MemoryStore 原子写入保障

所有关键写入使用统一模式:

```
MemoryStore._atomic_write(path, content)
  ├─ tmp = path.with_suffix(".tmp")
  ├─ tmp.write_text(content)
  ├─ os.fsync(tmp.fileno())        # 强制刷盘
  └─ os.replace(tmp, path)         # 原子替换 (POSIX 保证)
```

此模式应用于 `write_memory_file`, `append_history`, `_write_entries`, `write_soul`, `write_user`。

## 8. 混合搜索 (Hybrid Search)

### 8.1 概述

`HybridStore` (`memory/hybrid_store.py`) 为 MEMORY.md 和 history.jsonl 提供语义 + 关键词混合搜索，替代原有的纯子串匹配。

```
HybridStore
├── SQLite DB (workspace/memory/search.db)
│   ├── chunks         — 内容存储 (source, source_key, content, created_at, metadata)
│   ├── chunks_vec     — vec0 虚拟表，cosine distance 向量搜索
│   └── chunks_fts     — FTS5 虚拟表，BM25 关键词搜索
├── Embedding model    — all-MiniLM-L6-v2 (384-dim), 惰性加载单例
├── index_memory()     — 索引 MEMORY.md 每一行
├── index_history()    — 索引 history.jsonl 每个条目
├── search(query, k=5) — 混合搜索: 向量 + FTS5 融合评分
└── 时间衰减           — history.jsonl 条目指数衰减，MEMORY.md 永久豁免
```

### 8.2 评分融合

```
# 向量路径: cosine distance → similarity
vec_sim = max(0, 1 - cosine_distance / 2)

# FTS5 路径: BM25 rank → 归一化评分
text_score = 1 / (1 + exp(bm25_rank / 100))

# 加权融合 (0.7 向量 / 0.3 文本)
final_score = 0.7 * vec_sim + 0.3 * text_score

# 时间衰减 (仅 history.jsonl，MEMORY.md 豁免)
age_days = (now - created_at).days
decay = exp(-ln(2) / 30 * age_days)
decayed_score = final_score * decay
```

### 8.3 索引触发

| 操作 | 触发点 | 文件:行 |
|------|--------|---------|
| MEMORY.md 写入 | `MemoryStore.write_memory_file()` 之后 | `store.py:97` |
| history.jsonl 追加 | `MemoryStore.append_history()` 之后 | `store.py:132` |
| `remember()` | 委托 `store.write_memory_file()` | `context_manager.py:843` |
| `forget()` | 委托 `store.write_memory_file()` | `context_manager.py:861` |

### 8.4 搜索调用链

```
memory_recall 工具                                   tools/memory_tools.py:111
  └── ContextManager.recall(query)                   context_manager.py:880
        ├── [优先] HybridStore.search(query)          hybrid_store.py:248
        │     ├── _vector_search() → vec0 cosine     hybrid_store.py:260
        │     ├── _text_search()  → FTS5 BM25        hybrid_store.py:271
        │     ├── _fuse() → 0.7*vec + 0.3*text       hybrid_store.py:284
        │     └── _apply_temporal_decay()             hybrid_store.py:300
        └── [回退] _substring_recall()                context_manager.py:898
              (原有子串匹配，不依赖 SQLite/embeddings)
```

### 8.5 优雅降级

- **sqlite-vec 不可用**: 仅使用 FTS5 关键词搜索（`_has_vec = False`）
- **sentence-transformers 不可用**: 回退到 `_substring_recall()` 子串匹配
- **整个 HybridStore 创建失败**: `Orchestrator` 设 `hybrid_store = None`，`ContextManager.recall()` 自动使用子串回退
- **搜索异常**: `recall()` 捕获异常后回退到子串匹配

### 8.6 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HYBRID_SEARCH_ENABLED` | `true` | 启用混合搜索 |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers 模型名称 |

> 三种检索算法（向量检索、FTS5、子串匹配）的详细原理、算法伪代码、评分融合公式及全方位对比，见 **[memory-search-algorithms.md](memory-search-algorithms.md)**。

## 设计要点

- **LLM 替代关键词**: 用 LLM 摘要替代关键词匹配，提取的事实更准确，自然去重
- **两级时效性**: Consolidator（秒级延迟）+ Dream（2 小时合并），兼顾实时性和质量
- **异步不阻塞**: Consolidator fire-and-forget，Dream 后台执行，用户交互不受影响
- **自驱动 cron**: 后台 timer loop 不依赖用户输入，闲置时照样运行
- **原子写入**: 所有文件操作使用 tmp+fsync+replace，崩溃安全
