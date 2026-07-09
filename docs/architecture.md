# 架构概览 (Architecture)

## 高层架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        入口点 (Entry Points)                      │
│  mybot (CLI)  │  mybot-server (HTTP/WS)  │  外部 Channel (WeChat) │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Orchestrator (编排器)                         │
│  组合: MCPService + BackgroundService                             │
│  内联: ToolRegistry / SessionLifecycle / IdleCompression          │
└──────────┬────────────────────────────┬──────────────────────────┘
           │                            │
           ▼                            ▼
┌──────────────────────┐    ┌──────────────────────────┐
│   ContextManager     │    │      Dispatcher          │
│   组合:               │    │   四层路由决策:            │
│   SessionStore        │    │   L1: 显式命令 (/react)    │
│   MemoryService       │    │   L2: 关键词启发          │
│   内联: PromptBuilder  │    │   L3: LLM 分类 (可选)     │
│   内联: CoreContext    │    │   L4: 默认 (react)        │
└──────────┬───────────┘    └──────────┬───────────────┘
           │                            │
           ▼                            ▼
┌──────────────────────────────────────────────────────────────┐
│                    Agent (范式层)                               │
│  ReActAgent  │  PlanSolveAgent  │  DeepResearchAgent          │
│  单一 loop     │  两阶段 loop       │  orchestrator-workers     │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                    AgentCore (执行运行时)                       │
│  LLM 调用 → 工具调用 → 结果回传 → 循环                          │
│  并行/串行工具执行  │  上下文压缩  │  错误恢复  │  停滞检测      │
└──────────────────────────────────────────────────────────────┘
```

## 请求流 (Request Flow)

```
HTTP/WS or CLI → Orchestrator.process_message()
  │
  ├─ ContextManager.build_messages()
  │   ├─ 修复中断的会话 (unmatched tool calls, missing responses)
  │   ├─ 组装 system prompt (base + memory + skills + tools + summaries)
  │   ├─ 加载会话历史 (cursor-based, 100-msg cap)
  │   └─ token-budget 检查 → 超限则压缩
  │
  ├─ Dispatcher.resolve()
  │   ├─ Layer 1: 显式命令 (/react, /plan, /research)
  │   ├─ Layer 2: 关键词启发 (multi-step → plan_solve)
  │   ├─ Layer 3: LLM 分类 (可选, 廉价模型)
  │   └─ Layer 4: 默认 (react)
  │
  ├─ Agent.run(AgentInput)
  │   └─ AgentCore.run() — 执行 loop
  │
  └─ ContextManager.save_exchange() → 持久化到磁盘
      └─ prune_by_count() 硬上限检查
```

## 核心组件

### Orchestrator (`core/orchestrator.py`)

主入口点，通过组合模式集成以下服务：

| 服务/模块 | 文件 | 职责 |
|-----------|------|------|
| `MCPService` | `core/mcp_service.py` | MCP 客户端生命周期 (setup/start/stop) |
| `BackgroundService` | `core/background_service.py` | cron 调度器、ScheduledTaskService、Dream pipeline |
| Tool registry (内联) | `core/orchestrator.py` | 工具自动发现、注册、查找 |
| Session lifecycle (内联) | `core/orchestrator.py` | 会话 CRUD、记忆操作、dispatcher 访问 |
| Idle compression (内联) | `core/orchestrator.py` | 空闲会话压缩、过期会话清理 |

`Orchestrator` 自身保留 `process_message()`、`serve()` 等核心编排逻辑，以及 `start_services()`/`stop_services()` 服务生命周期方法。所有公开 API 保持不变。

### ContextManager (`context/context_manager.py`)

统一上下文组装和压缩，通过组合模式集成以下服务：

| 服务/模块 | 文件 | 职责 |
|-----------|------|------|
| `SessionStore` | `context/session_store.py` | 会话保存/加载/删除/列表、过期清理 |
| `MemoryService` | `context/memory_service.py` | 记忆上下文构建、remember/forget/recall、混合搜索 |
| Prompt builder (内联) | `context/context_manager.py` | system prompt 组装、历史注入、文件上下文提取、中断修复 |
| Core context (内联) | `context/context_manager.py` | build_messages 主流程、压缩 (compress/full_compress) |

向后兼容别名: `self.session = self.session_store.session`, `self.store = self.memory.store`

关键行为:
- **压缩是非破坏性的**: `session.messages` 从不被修改。`CompactionService` 推进 `consolidated_cursor` 跳过旧消息；`Consolidator` 追加 LLM 摘要到 `memory/history.jsonl`
- **会话硬上限**: `prune_by_count()` 在 `save_exchange` 后触发，与会话摘要解耦
- **会话过期**: `purge_expired_sessions()` 基于文件 mtime，默认 TTL 30 天

### SessionManager (`context/session.py`)

会话的 JSON 持久化存储。每个会话是一个 `Session` dataclass，包含消息列表、consolidated_cursor 和元数据。提供:
- `get_session()` / `save_session()` — 获取/保存会话
- `add_message_to_session()` / `add_messages_to_session()` — 追加消息
- `get_session_history()` — 从 disk 加载或返回内存缓存
- `prune_by_count()` — 硬上限裁剪（与会话摘要解耦）
- `purge_expired_sessions()` — TTL 过期清理

### Dispatcher (`core/dispatcher.py`)

四层路由，将用户输入分配到正确的 Agent 范式:

| 层 | 机制 | 示例 |
|----|------|------|
| 1 | 正则匹配显式命令 | `/react`, `/plan`, `/research` |
| 2 | 关键词启发 | multi-step → plan_solve |
| 3 | LLM 分类 (可选) | 廉价模型 <10 token 响应 |
| 4 | 默认 | react |

### Agent 范式

| Agent | 文件 | 模式 |
|-------|------|------|
| `ReActAgent` | `agents/react_agent.py` | 单次 AgentCore loop |
| `PlanSolveAgent` | `agents/plan_solve_agent.py` | 两阶段: plan → execute (两个串联 loop) |
| `DeepResearchAgent` | `agents/deep_research_agent.py` | orchestrator-workers: lead 分解 → 并行 worker → synthesis |

所有 Agent 遵循统一契约: `run(AgentInput) -> AgentOutput`，由 `discover_agents()` 自动发现。

### AgentCore (`core/runner.py`)

所有 Agent 范式共享的执行运行时:
- LLM 调用 loop + 工具调用 + 结果回传
- 并行 vs 串行工具执行 (`asyncio.gather` 用于 parallel-safe 工具)
- 7 步上下文压缩 (remove orphans → fill missing → summarize old → truncate long → token budget → repair)
- LLM 错误恢复 (context_length → compact & retry; content_filter → compliance hint & retry)
- 50+ 步停滞检测
- Middleware hook: agent start/step/end, LLM call, tool execute

### Middleware (`core/middleware.py`)

Chain-of-responsibility 模式。`MiddlewareChain` 嵌套 `call_next` 闭包，使 middleware[n] 包装 middleware[n+1]。5 个 hook 点:
- `on_agent_start` — agent 启动时
- `on_agent_step` — 每步执行前（可中止 loop）
- `on_llm_call` — LLM 调用前后（可修改 messages/model）
- `on_tool_execute` — 工具执行前后（可阻止/修改/缓存结果）
- `on_agent_end` — agent 结束时

### 工具系统 (`tools/`)

| 组件 | 职责 |
|------|------|
| `Tool` (ABC) | 每个工具继承此基类: `name`, `description`, `parameters` (JSON Schema), `capabilities`, `_scopes`, `_parallel` |
| `ToolRegistry` | 注册、查找、列出工具 |
| `ToolGuard` | 安全门禁: capability → 安全策略 (SHELL → 注入检测, NETWORK → SSRF 检查, FILE_READ/WRITE → 敏感路径阻止) |
| `discover_tools()` | 自动发现: `pkgutil.iter_modules` + `inspect.getmembers` |

### Provider 层 (`providers/`)

| 组件 | 职责 |
|------|------|
| `LLMProvider` (ABC) | 抽象基类: `chat()`, `safe_chat()`, `chat_with_retry()`, `chat_stream()` (SSE), `chat_stream_with_retry()` |
| `OpenAICompatibleProvider` | 与任何 OpenAI-compatible API 协同工作。懒初始化 `AsyncOpenAI` 客户端 (async lock 保护)。OpenRouter 自动检测。 |

### 记忆系统 (`memory/`)

| 组件 | 职责 |
|------|------|
| `MemoryStore` | 长期文件存储 + 混合搜索 (SQLite FTS5 + sqlite-vec) |
| `Consolidator` | 每轮 LLM 摘要 → `memory/history.jsonl` |
| Dream pipeline | 后台记忆整合、去重、年龄标注 |

### 配置 (`config/`)

三源优先级: **shell 环境变量 > settings.json > .env 文件**

`Config` 类将所有配置项作为类属性暴露，类型化且集中管理。`settings.json` 支持基于 fnmatch 模式的 per-model 上下文窗口覆盖。

`Config.reload()` 重载 `.env` 和 `settings.json`，确保配置变更无需重启即可生效。

## 包布局

```
mybot/                          # 扁平布局 (no mybot/ subdirectory)
├── core/                       # Orchestrator, Dispatcher, AgentCore, Middleware, Server
│   ├── orchestrator.py         #   主入口 + 内联的 ToolRegistry/SessionLifecycle/IdleCompression
│   ├── mcp_service.py          #   MCP 客户端生命周期
│   ├── background_service.py   #   CronScheduler + ScheduledTaskService + Dream
│   ├── runner.py               #   AgentCore — 共享执行 loop
│   ├── dispatcher.py           #   四层路由
│   ├── middleware.py            #   Chain-of-responsibility
│   ├── agent_base.py           #   BaseAgent ABC
│   ├── server.py               #   HTTP/WS 服务器
│   ├── events.py               #   事件总线
│   └── skills.py               #   SkillsLoader
├── context/                    # 上下文管理
│   ├── context_manager.py      #   ContextManager + 内联的 PromptBuilder/CoreContext
│   ├── session_store.py        #   SessionStore — 会话持久化
│   ├── memory_service.py       #   MemoryService — 记忆 CRUD + 混合搜索
│   ├── session.py              #   SessionManager + Session dataclass
│   ├── compaction.py           #   CompactionService (cursor advancement, no LLM)
│   └── token_budget.py         #   TokenBudget (single config source)
├── agents/                     # Agent 范式 (自动发现)
│   ├── react_agent.py          #   ReAct — 单一 loop
│   ├── plan_solve_agent.py     #   Plan-Solve — 两阶段
│   ├── deep_research_agent.py  #   DeepResearch — orchestrator-workers
│   └── team/                   #   多智能体机制层 (子包，不被自动发现扫描)
├── providers/                  # LLM 后端抽象
│   ├── base.py                 #   LLMProvider ABC
│   ├── openai_compatible_provider.py
│   └── factory.py              #   工厂函数
├── tools/                      # 工具系统
│   ├── tool.py                 #   Tool ABC + ToolRegistry
│   ├── guard.py                #   ToolGuard
│   ├── registry.py             #   自动发现
│   ├── mcp/                    #   MCP 客户端
│   └── sandbox/                #   沙盒工具
├── memory/                     # 长期记忆
│   ├── store.py                #   MemoryStore + 混合搜索
│   ├── consolidator.py         #   Consolidator (LLM 摘要)
│   └── dream.py                #   Dream pipeline
├── services/                   # 后台服务
│   ├── cron.py                 #   CronScheduler (定时器驱动)
│   └── scheduled_tasks.py      #   ScheduledTaskService
├── observability/              # 可观测性
│   ├── log.py                  #   loguru 结构化日志
│   ├── metrics.py              #   Prometheus 风格指标
│   └── trace.py                #   span 追踪
├── config/                     # 配置
│   ├── config.py               #   Config 类 (所有配置项)
│   └── settings.py             #   settings.json 加载 + 阈值/模型配置
├── utils/                      # 工具函数
│   └── templates.py            #   Jinja2 模板渲染
├── prompt_templates/           # 14 个 Agent prompt 模板 (Jinja2 .md)
├── server_web/                 # 浏览器聊天 UI (index.html)
└── docs/                       # 项目文档
```

## 依赖图

```
Orchestrator
  ├── ContextManager
  │   ├── SessionStore → SessionManager
  │   ├── MemoryService → MemoryStore, HybridStore
  │   ├── CompactionService, Consolidator, TokenBudget, SkillsLoader
  ├── MCPService → MCPClientManager
  ├── BackgroundService → CronScheduler, ScheduledTaskService, Dream
  ├── Dispatcher, ToolRegistry → ToolGuard
```

## 关键设计决策

1. **组合优于继承**: Orchestrator 和 ContextManager 通过依赖注入组合独立服务类 (`SessionStore`, `MemoryService`, `MCPService`, `BackgroundService`)，而非多继承 mixin。每个服务类高内聚低耦合，可独立测试。公开 API 向后兼容。

2. **会话硬上限与摘要解耦**: `prune_by_count()` 在每次消息交换后触发，不依赖 consolidation 成功。consolidation 失败的场景下仍能防止内存泄漏。

3. **压缩是非破坏性的**: session.messages 从不被修改。通过推进 consolidated_cursor 实现，LLM 摘要写入独立的 history.jsonl。

4. **Agent 范式可插拔**: 新 Agent 范式只需实现 `BaseAgent.run()`，被 `discover_agents()` 自动发现。Dispatcher 通过四层路由选择合适的范式。

5. **统一 I/O 契约**: `AgentInput` / `AgentOutput` 作为所有 Agent 范式、Middleware、AgentCore 之间的通用数据类型。

6. **配置三源优先级**: shell env > settings.json > .env，所有配置通过 `Config` 类集中访问，禁止直调 `os.getenv()`。
