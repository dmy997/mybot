# mybot Code Reading Guide

全量阅读 mybot 代码库的指南，按数据流分层组织，约 75 个 Python 源文件。

## 快速导航

| 想了解 | 从哪开始 |
|--------|----------|
| 请求如何被处理 | Layer 8: Orchestrator → Layer 5: AgentCore |
| 消息如何发送给 LLM | Layer 3: Provider → Layer 5: AgentCore |
| 工具如何被执行 | Layer 4: Tools → Layer 5: AgentCore |
| 会话如何持久化 | Layer 8: Context Management |
| 记忆如何被检索 | Layer 9: Memory |
| HTTP/WS 如何工作 | Layer 11: Server + Web UI |
| 微信如何接入 | Layer 12: Channels |
| 多 Agent 如何协作 | Layer 6: Agent Paradigms |

---

## Layer 1: Configuration（配置层）

**阅读顺序：settings.py → config.py**

### `config/settings.py` (265 行)
全局设置管理。`Settings` 类负责 `~/.mybot/settings.json` 的加载、自动生成和按优先级查找：
- `resolve(key, default)` — 优先级：shell env > settings.json > 默认值
- `model_config_for(name)` — fnmatch 模式匹配模型配置（context_window、max_output_tokens）
- `thresholds` — 压缩/预算/空闲相关阈值
- 首次运行自动生成带有默认值的 settings.json

### `config/config.py` (356 行)
`Config` 类，所有环境变量的集中入口。每个配置项通过 `@property` 暴露：
- LLM 配置：`api_key`, `api_base`, `provider_name`, `default_model`, `light_model`
- 窗口配置：`context_window`, `max_output_tokens`
- 阈值配置：`warning_buffer_ratio`, `compress_ratio`, `idle_compress_seconds`
- 服务配置：`host`, `port`, `api_key` (Bearer 认证)
- 混合搜索：`hybrid_search_enabled`, `embedding_model`

---

## Layer 2: Core Message Models（核心消息模型）

**阅读顺序：message_bus.py → events.py → session_context.py**

### `core/message_bus.py` (176 行)
消息总线的数据结构：
- `InboundMessage` — 入站消息（`session_key`, `message`, `images`, `files`, `msg_type`, `metadata`）
- `OutboundMessage` — 出站消息（同上 + `sender_type`, `paradigm`）
- `MessageBus` — 每会话一个 `asyncio.Queue` 入站 + 每通道一个出站队列

### `core/events.py` (257 行)
异步发布/订阅事件总线：
- `EventBus` — 全局单例，`subscribe(event_type, callback)` / `emit(event_type, **data)`
- 事件类型：`agent.start`, `agent.step`, `agent.end`, `llm.call`, `tool.execute`, `memory.search`
- 用于 observability 子系统的指标/Trace 收集

### `core/session_context.py` (45 行)
轻量级请求上下文：`SessionContext` dataclass，携带 `session_key`, `trace_id`, `span_id`, `channel_type`，通过 `contextvars` 在整个请求生命周期中传播。

---

## Layer 3: LLM Provider（LLM Provider 层）

**阅读顺序：base.py → errors.py → retry.py → openai_compatible_provider.py**

### `providers/base.py` (207 行)
`LLMProvider` 抽象基类：
- `chat(messages, tools, model, ...)` → `LLMResponse` — 单次调用
- `safe_chat()` — 错误包装为 `LLMResponse`
- `chat_with_retry()` — 带重试
- `chat_stream(callback, ...)` — 真实 SSE 流式（delta 回调）
- `chat_stream_with_retry()` — 流式+重试
- 响应类型：`LLMResponse`（content, tool_calls, usage, stop_reason）, `LLMUsage`（prompt_tokens, completion_tokens）, `StreamChunk`（content, tool_call）

### `providers/errors.py` (68 行)
错误分类：`LLMError` 基类 + `LLMTimeoutError`, `LLMAuthError`, `LLMRateLimitError`, `LLMContextLengthError`, `LLMContentFilterError`, `LLMProviderError`。

### `providers/retry.py` (135 行)
指数退避重试：`_should_retry()` 判断错误是否可重试（rate_limit, server_error, timeout），`_compute_delay()` 计算退避延迟，`chat_with_retry()` 装饰器模式。

### `providers/openai_compatible_provider.py` (710 行)
`OpenAICompatibleProvider(LLMProvider)` — 核心实现：
- 惰性 `AsyncOpenAI` 客户端（async lock 保护）
- OpenRouter 自动检测（设置 referer 和 session-affinity 头）
- `_build_tools()` — JSON Schema → OpenAI tool 格式
- `_parse_response()` — OpenAI response → `LLMResponse`
- `_parse_tool_calls()` — 累积式流式 tool call delta
- 工具结果映射回 message 格式

---

## Layer 4: Tools System（工具系统）

**阅读顺序：tool.py → registry.py → guard.py → 具体工具文件**

### `tools/tool.py` (83 行)
`Tool` 抽象基类：
- `name`, `description`, `parameters`（JSON Schema）
- `capabilities`（SHELL, FILE_READ, FILE_WRITE, NETWORK, MEMORY, DELEGATE）
- `_scopes`（作用域，控制哪些 Agent 可见）
- `_parallel`（标记工具是否可并行执行）
- `execute(input: ToolInput) → ToolOutput`

### `tools/registry.py` (92 行)
`ToolRegistry` — 工具注册中心：
- `discover_tools()` — `pkgutil.iter_modules` + `inspect.getmembers` 自动发现
- `get_tool(name)`, `list_tools(scope)`, `get_schemas(scope)`

### `tools/guard.py` (303 行)
`ToolGuard` — 工具安全边界：
- 能力→安全检查映射：SHELL→注入检测、NETWORK→SSRF 检查、FILE_READ/WRITE→敏感路径阻止
- `check(tool, input)` — 同步检查；`check_async(tool, input)` — 异步（含网络检查）

### 具体工具（约 2500 行）
- **`tools/bash_tool.py`** (228 行) — Bash 命令执行，带超时和沙箱选项
- **`tools/file_tools.py`** (459 行) — 文件读写（ReadTool, WriteTool, EditTool）
- **`tools/grep_tool.py`** (185 行) — 代码搜索（基于 ripgrep）
- **`tools/webfetch_tool.py`** (138 行) — 网页抓取（HTTP→Markdown）
- **`tools/websearch_tool.py`** (487 行) — 网络搜索（多引擎）
- **`tools/memory_tools.py`** (167 行) — 长期记忆 CRUD
- **`tools/subagent.py`** (151 行) — 子 Agent 委托
- **`tools/schedule_task.py`** (155 行) — 定时任务创建/管理
- **`tools/xiaohongshu_publish.py`** (217 行) — 小红书自动发布

---

## Layer 5: AgentCore（Runner 执行循环）

**单文件阅读：`core/runner.py` (1279 行)**

所有 Agent 范式的共享执行循环，是整个框架的心脏：

1. **AgentInput/AgentOutput** — 通用 I/O 合约，携带 messages + tools + 流式回调
2. **`AgentCore.run()`** — 主循环：
   - LLM 调用 → 解析响应 → 并行/串行执行工具 → 结果喂回 → 循环
   - 并行工具执行：`asyncio.gather` 并行安全工具
   - 串行工具执行：按序执行，前一个输出可作后一个输入
3. **上下文压缩** — token 超预算时触发 3 步压缩流水线
4. **错误恢复**：
   - `context_length` → 压缩后重试
   - `content_filter` → 追加合规提示后重试
5. **停滞检测** — 15+ 步时提示，50+ 步时终止
6. **中间件钩子** — agent.start / agent.step / agent.end / llm.call / tool.execute

---

## Layer 6: Agent Paradigms（Agent 范式）

**阅读顺序：agent_base.py → react_agent.py → plan_solve_agent.py → deep_research_agent.py → __init__.py**

### `core/agent_base.py` (72 行)
`BaseAgent` ABC — 每个范式只描述*发送什么消息*，绝不直接调用 LLM API：
- `run(AgentInput) → AgentOutput`

### `agents/react_agent.py` (22 行)
ReAct（单轮推理+行动）：将 system prompt + messages + tools 作为一次 LLM 调用发送，最简范式。

### `agents/plan_solve_agent.py` (82 行)
Plan-and-Solve（先规划后执行）：第一阶段 LLM 生成执行计划，第二阶段逐步执行计划。

### `agents/deep_research_agent.py` (155 行)
多 Agent 深度研究流水线：orchestrator agent 分解研究问题 → worker agent 并行搜索 → synthesizer agent 汇总报告。

### `agents/__init__.py` (56 行)
`discover_agents()` — `pkgutil.iter_modules` + `inspect.getmembers` 自动发现所有 `BaseAgent` 子类。

---

## Layer 7: Dispatcher + Middleware（调度与中间件）

**阅读顺序：dispatcher.py → middleware.py**

### `core/dispatcher.py` (298 行)
四层路由系统：
- Layer 1：显式命令（`/react`、`/plan`）
- Layer 2：关键词启发式（多步骤→plan_solve）
- Layer 3：LLM 分类（可选，使用轻量模型）
- Layer 4：默认路由（react）

### `core/middleware.py` (202 行)
责任链模式中间件：
- `MiddlewareChain` — 嵌套 `call_next` 闭包，`middleware[n]` 包裹 `middleware[n+1]`
- 五个钩子点：`on_agent_start`, `on_agent_step`（可中止循环）, `on_llm_call`（修改消息/模型）, `on_tool_execute`（阻止/修改/缓存结果）, `on_agent_end`
- `MiddlewareContext.data` — 共享可变状态

---

## Layer 8: Context Management（上下文管理）

**阅读顺序：session.py → session_store.py → token_budget.py → compaction.py → context_manager.py → memory_service.py**

### `context/session.py` (304 行)
`SessionManager` — JSON 文件持久化的会话管理：
- `save_exchange()` / `load_messages()` — 基于游标的增量加载
- `prune_by_count()` — 硬上限裁剪（默认 2000 条，旧消息先裁剪）
- `purge_expired_sessions()` — TTL 过期清理（默认 30 天）
- `_repair_session()` — 中断修复（3 种模式：未匹配 tool_call、缺失 assistant 响应）

### `context/session_store.py` (78 行)
`save_exchange()` 的用户输入类型：`str | list[dict]`（支持多模态 content-part 数组）。

### `context/token_budget.py` (93 行)
Token 预算计算：基于模型 `context_window` 和 `max_output_tokens` 计算 `effective_window`、`warning_threshold`、`auto_compact_threshold`、`block_threshold`。

### `context/compaction.py` (389 行)
`CompactionService` — 非破坏性压缩：
- `session.messages` 永不修改
- `consolidated_cursor` 推进以跳过旧消息
- `Consolidator` 将 LLM 摘要追加到 `memory/history.jsonl`
- 7 步压缩流水线：移除孤儿→填充缺失→摘要旧消息→截断长消息→token 预算→修复

### `context/context_manager.py` (772 行)
四个 mixin 组合而成的综合管理器：
- `CoreContextMixin` — `build_messages(images, context_window, max_output_tokens)` 构建消息列表（含多模态 content-part 数组）
- `PromptBuilderMixin` — system prompt 组装（base + skills + tools + memory + file + recent history）
- `MemoryOperationsMixin` — remember/forget/recall 操作
- `SessionPersistenceMixin` — session CRUD + TTL 过期

### `context/memory_service.py` (169 行)
会话间记忆服务：从 session 上下文提取信息到长期记忆。

---

## Layer 9: Long-term Memory（长期记忆）

**阅读顺序：types.py → store.py → hybrid_store.py → consolidator.py → dream.py**

### `memory/types.py` (6 行)
`MemoryType` 枚举：`USER`, `FEEDBACK`, `PROJECT`, `REFERENCE`。

### `memory/store.py` (316 行)
`MemoryStore` — 基于文件的类型化记忆 I/O：
- 两步写入：写 `.md` 文件（frontmatter + body）→ 更新 `MEMORY.md` 索引
- `hybrid_search(query)` — 委托给 `HybridStore`

### `memory/hybrid_store.py` (500 行)
`HybridStore` — SQLite + sqlite-vec + FTS5 混合搜索：
- sqlite-vec：384 维向量相似度（cosine）
- FTS5 BM25：关键词匹配
- 分数融合：0.7 × vector + 0.3 × BM25
- 时间衰减：30 天半衰期指数衰减
- 可观测性：`MemorySearchEvent` 发出到 EventBus

### `memory/consolidator.py` (279 行)
`Consolidator` — LLM 驱动的实时摘要：在会话进行中增量合并记忆。

### `memory/dream.py` (415 行)
Dream 系统 — 两阶段记忆合并：
- 阶段 1：Consolidator 实时摘要
- 阶段 2：Dream 周期回顾（空闲时触发，合并和去重记忆）

---

## Layer 10: Orchestrator（协调器）

**单文件阅读：`core/orchestrator.py` (994 行) + `core/orchestrator_mixins.py`**

### `core/orchestrator_mixins.py`
四个 mixin 拆分 Orchestrator 关注点：
- `MCPServicesMixin` — MCP 服务器 + cron + 定时任务
- `ToolRegistryMixin` — 工具发现/注册
- `SessionLifecycleMixin` — session CRUD + memory + dispatcher
- `IdleCompressionMixin` — 空闲压缩 + session 过期

### `core/orchestrator.py` (994 行)
顶层协调器——CLI 交互循环、HTTP API、请求生命周期：
- `process_message(session_key, message, images, ...)` — 核心入口
- `serve(session_key)` — 后台消费 MessageBus 入站队列
- `run(session_key)` — 交互式 CLI 循环（Rich Live 渲染）
- 图片多模态：`_model_supports_vision()` 检查模型是否支持图片
- 模型不匹配时返回友好错误消息（不自作主张切换模型）

---

## Layer 11: HTTP/WS Server + Web UI（服务层）

**阅读顺序：server.py → server_web/index.html**

### `core/server.py` (803 行)
FastAPI/Starlette HTTP + WebSocket 服务器：
- `GET /` — 返回 Web UI
- `POST /chat/{session_key}` — SSE 流式聊天
- `POST /chat/{session_key}/push` — SSE 推送（长连接轮询）
- `WS /ws/{session_key}` — WebSocket 双向通信
- `POST /hitl/respond` — 人机交互确认
- SSE 事件类型：`delta`（`{"token": "..."}`）、`tool_call_delta`、`tool_execute_start`、`tool_execute_end`、`done`（含 content/stop_reason/paradigm/error/usage/metrics）

### `server_web/index.html` (1475 行)
单文件 Web 聊天 UI：
- 会话管理（新建/切换/删除会话）
- 流式消息渲染（marked.js Markdown 解析 + highlight.js 代码高亮）
- 图片上传（粘贴/拖拽 → FileReader → base64 data URL）
- 人机交互确认对话框
- WebSocket 作为 SSE 的备选方案
- CSS 变量主题系统

---

## Layer 12: Channels + Services（频道与服务）

**阅读顺序：base.py → wechat.py → cron.py → hitl.py → scheduled_tasks.py**

### `channels/base.py` (171 行)
`BaseChannel` ABC — 频道抽象：
- `ChannelMessage` — 频道消息（含 `images`）
- `send_reply()`, `build_session_key()`
- `_process_message()` — 从 `ChannelMessage` 构建 `InboundMessage` 并入队

### `channels/wechat.py` (1297 行)
`WechatChannel` — 微信 iLink 机器人频道：
- iLink API 接入（扫码授权）
- 消息类型处理：文本、图片（`ITEM_IMAGE` type=3）、语音等
- 图片下载到 inbox → 转 base64 data URL → 传入 `ChannelMessage.images`

### `services/cron.py` (317 行)
`CronScheduler` — 自驱定时器：
- cron 表达式 + 固定间隔两种模式
- 单线程执行，下次触发时间自动计算

### `services/hitl.py` (297 行)
人机交互服务：
- `HitlMiddleware` — AgentMiddleware，在 `on_tool_execute` 中暂停执行
- `HitlService` — 使用 `asyncio.Future` 阻塞直到用户批准/拒绝
- 模式：`confirm`（需要确认）、`bypass`（自动执行）

### `services/scheduled_tasks.py` (238 行)
定时任务服务：聊天创建的任务 + 系统副作用任务（如小红书自动发布）。

### `services/xiaohongshu.py` (41 行)
小红书自动发布入口：通过 CronScheduler 定时触发海龟汤谜题发布。

---

## Layer 13: Observability（可观测性）

**阅读顺序：log.py → metrics.py → trace.py → subscribers.py → display.py → stream_renderer.py → otel_bridge.py → persistence.py → recent.py**

### `observability/log.py` (210 行)
基于 loguru 的结构化日志，所有事件携带类型化字段（`event_type`, `trace_id`, `span_id`, `latency_ms`）。

### `observability/metrics.py` (281 行)
Prometheus 风格指标：`Counter`, `Gauge`, `Histogram`，全局 `REGISTRY` 单例，`REGISTRY.collect_all()` 获取快照。

### `observability/trace.py` (253 行)
基于 `contextvars` 的 span 传播：`with tracer.span("llm.chat", model="gpt-4"): ...`，span 结束时以结构化日志事件输出。

### `observability/subscribers.py` (124 行)
将 Agent/LLM/Tool 生命周期事件桥接到指标和日志的订阅者。

### `observability/display.py` (250 行)
Rich 终端 UI 日志显示。基于 Rich Console 的多面板布局。

### `observability/stream_renderer.py` (173 行)
Rich Live 流式渲染器：实时显示 token 输出、工具调用状态。

### `observability/otel_bridge.py` (183 行)
OpenTelemetry 桥接：将自定义 tracer 的 span 镜像到 OTel SDK，通过 OTLP HTTP 导出到 Jaeger。

### `observability/persistence.py` (257 行) + `observability/recent.py` (109 行)
日志持久化和近期事件查询。

---

## Layer 14: Skills + Templates + TUI

### `core/skills.py` (242 行)
`SkillsLoader` — 基于文件的 Skill 发现（YAML frontmatter），自动注入 system prompt。

### `prompt_templates/` (18 个模板)
Jinja2 模板（`.md` 格式，`strip=True`）：
- `SOUL.md` — 核心人格 prompt
- `AGENTS.md` — Agent 能力描述
- `HEARTBEAT.md` — 周期性任务检查清单
- `USER.md` — 用户信息模板

### `utils/utils.py` (85 行)
`render_template(name, **vars)` — Jinja2 模板渲染。

### `utils/images.py` (56 行)
图片工具：`file_to_data_url()`, `SUPPORTED_IMAGE_EXTENSIONS`, `MAX_IMAGE_BYTES`。

### `evals/` (约 7 个文件)
Agent 评估系统：自定义 YAML 任务 + 规则评分器 + BFCL/GAIA 基准。

---

## Layer 15: Test Suite（测试套件）

**`test/` 目录，50 个测试文件，约 961 个测试用例**

按模块组织：
- `test/providers/` — Provider 层测试
- `test/core/` — Orchestrator, AgentCore, Server, Middleware, Dispatcher 测试
- `test/context/` — ContextManager, Session, Compaction 测试
- `test/tools/` — 各工具测试
- `test/memory/` — MemoryStore, HybridStore, Consolidator 测试
- `test/channels/` — BaseChannel, WechatChannel 测试
- `test/services/` — CronScheduler, HitlService 测试
- `test/observability/` — 可观测性测试

使用 `pytest-asyncio`（`asyncio_mode = "auto"`）。

---

## 阅读策略建议

1. **如果你想理解"一条消息如何变成回复"**：按 Layer 1→2→3→5→8→10 阅读
2. **如果你想添加新工具**：看 Layer 4 的 `tool.py` + `registry.py` + 任一具体工具
3. **如果你想添加新 Agent 范式**：看 Layer 6 的 `agent_base.py` + `react_agent.py`
4. **如果你想接入新消息频道**：看 Layer 12 的 `base.py` + `wechat.py`
5. **如果你想优化上下文管理**：看 Layer 8 的 `context_manager.py` + `compaction.py`
6. **如果你想了解可观测性**：看 Layer 13 的 `trace.py` + `metrics.py` + `otel_bridge.py`

每个文件都有清晰的模块文档字符串和类型注解，直接打开文件从第一行开始读即可。
