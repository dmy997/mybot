# mybot

<p align="center">
  <img src="assets/mybot-logo.svg" alt="mybot logo" width="128" height="128">
</p>

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-961%20passed-green)](.)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

受 **Claude Code**、**nanobot**、**OpenClaw** 启发，通过 Claude Code vibe coding 开发的个人 AI 助手框架。可读性强，高度模块化，轻量无冗余。

**中文** | [English](./README_en.md)

## 核心特性

- **多 Provider** — 兼容任意 OpenAI 兼容 API（OpenRouter、DeepSeek、本地模型）
- **范式 Agent** — ReAct（单轮推理+行动）、Plan-and-Solve（先规划后执行）；通过 `discover_agents()` 自动发现
- **流式输出** — SSE、WebSocket、Rich 终端 UI，实时渲染工具调用
- **可插拔中间件** — 责任链模式，拦截 LLM 调用、工具执行、Agent 生命周期
- **长期记忆** — 基于文件的类型化记忆（用户/反馈/项目/参考），支持混合搜索（向量+FTS5）
- **上下文管理** — 非破坏性压缩、会话中断修复、空闲自动压缩
- **可观测性** — 结构化日志（loguru）、自定义指标/追踪，以及可选的 OpenTelemetry → Jaeger 桥接
- **断点恢复** — 长任务崩溃后可从检查点续跑，避免重新推理
- **多频道接入** — 可扩展频道架构（BaseChannel ABC），支持 CLI、Web UI、微信 iLink 机器人；MessageBus 每通道独立出站队列，消息不丢失
- **13 个内置 Skill** — docx、pptx、pdf、xlsx、canvas-design、frontend-design、algorithmic-art、brand-guidelines、internal-comms、mcp-builder、skill-creator、slack-gif-creator、theme-factory、web-artifacts-builder、webapp-testing

## 架构

### 请求流程

```
HTTP/WS/WeChat 或 CLI → Orchestrator → ContextManager.build_messages()
                                   ├─ 修复中断会话
                                   ├─ 组装 system prompt
                                   ├─ 加载会话历史
                                   └─ token 预算检查 → 超出则压缩
                → Dispatcher.resolve()
                    ├─ 第一层：显式命令（/react、/plan）
                    ├─ 第二层：关键词启发式匹配
                    ├─ 第三层：LLM 分类（可选）
                    └─ 第四层：默认路由（react）
                → Agent.run(AgentInput) → AgentCore.run()
                    └─ 循环：LLM 调用 → 工具调用（并行+串行）→ 结果喂回
                → ContextManager.save_exchange() → 持久化到磁盘
```

### 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| Orchestrator | `core/orchestrator.py` | 顶层协调器——CLI 交互循环、HTTP API、请求生命周期管理 |
| Dispatcher | `core/dispatcher.py` | 四层路由：命令 → 启发式 → LLM 分类 → 默认 |
| AgentCore | `core/runner.py` | 共享执行循环——流式输出、工具执行、上下文压缩、错误恢复 |
| Middleware | `core/middleware.py` | 可插拔中间件链——拦截 LLM 调用、工具执行、Agent 生命周期 |
| EventBus | `core/events.py` | 异步发布/订阅事件总线——Agent/LLM/Tool 生命周期事件 |
| MessageBus | `core/message_bus.py` | 每会话入站 + 每通道出站队列——不同频道消息完全隔离 |
| ContextManager | `context/context_manager.py` | 会话持久化、空闲压缩、token 预算压缩、中断修复 |
| MemoryStore | `memory/store.py` | 类型化长期记忆的文件 I/O |
| BaseChannel | `channels/base.py` | 频道抽象——ChannelMessage、send_reply、build_session_key |
| WechatChannel | `channels/wechat.py` | 微信 iLink 机器人频道——iLink API → MessageBus → Orchestrator |
| SkillsLoader | `core/skills.py` | 基于文件的 Skill 发现（YAML），自动注入 system prompt |

### Agent 范式

| 范式 | 说明 |
|------|------|
| `react` | 单轮推理+行动循环 |
| `plan_solve` | 先规划再执行，两阶段 |

## 安装

```bash
pip install -e ".[dev,server]"
cp .env.example .env   # 然后填入 API 密钥
```

可选依赖：

```bash
pip install -e ".[otel]"     # OpenTelemetry → Jaeger 桥接
```

## 快速开始

```bash
# 交互式 CLI
mybot

# HTTP/WS 服务器
mybot-server
# 浏览器打开 http://127.0.0.1:8080

# 微信 iLink 机器人
mybot-wechat
# 终端显示二维码，手机微信扫码授权

# WebSocket
websocat ws://127.0.0.1:8080/ws/default
```

### 编程方式调用

```python
import asyncio
from config import Config
from core.middleware import AgentMiddleware, MiddlewareChain
from core.orchestrator import Orchestrator
from providers.openai_compatible_provider import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(
    api_key=Config.api_key,
    api_base=Config.api_base,
    name=Config.provider_name,
    default_model=Config.default_model,
)

orche = Orchestrator(
    workspace=Config.workspace,
    provider=provider,
    compress_model=Config.light_model,
)

# 单条消息
result = await orche.process_message("default", "你好")
print(result.content)

# 交互式循环
await orche.run("default")
```

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENAI_API_KEY` | — | API 密钥 |
| `OPENAI_API_BASE` | — | API 地址 |
| `PROVIDER_NAME` | `openrouter` | Provider 标识 |
| `LLM_MODEL_ID` | `deepseek/deepseek-v4-flash` | 默认模型 |
| `LIGHT_MODEL_NAME` | 同上 | 压缩/分类用的小模型 |
| `LLM_TIMEOUT` | `60` | 请求超时（秒） |
| `WORKSPACE` | `~/.mybot/workspace` | 工作目录 |
| `MYBOT_API_KEY` | — | HTTP/WS Bearer 认证密钥（不设则不校验） |
| `MYBOT_HOST` | `127.0.0.1` | 服务绑定地址 |
| `MYBOT_PORT` | `8080` | 服务端口 |
| `MYBOT_CHECKPOINT` | — | 启用长任务断点恢复 |
| `MYBOT_OTEL_ENABLED` | — | 启用 OpenTelemetry 桥接 |
| `MYBOT_OTEL_ENDPOINT` | `http://localhost:4318/v1/traces` | OTLP HTTP 端点 |

## 可观测性

mybot 提供两种互补的可观测性方案。

### 1. 内置可观测性（零外部依赖）

开箱即用，无需任何外部服务：

- **结构化日志**（`observability/log.py`）——基于 loguru，所有事件携带类型化字段（`event_type`、`trace_id`、`span_id`、`latency_ms`）
- **指标**（`observability/metrics.py`）——内存中的 `Counter` / `Gauge` / `Histogram`，全局 `REGISTRY` 单例，通过 `REGISTRY.collect_all()` 获取快照
- **追踪**（`observability/trace.py`）——基于 `contextvars` 的 span 传播，`with tracer.span("llm.chat", model="gpt-4"): ...`，span 结束时以结构化日志事件输出
- **事件订阅者**（`observability/subscribers.py`）——将 Agent/LLM/Tool 生命周期事件桥接到指标和日志

### 2. 可视化面板（OpenTelemetry → Jaeger）

只需一个环境变量即可启用完整的 trace 可视化流水线：

```bash
# 1. 安装 OTel 依赖
pip install "mybot[otel]"

# 2. 启动 Jaeger（一行 Docker 命令）
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one

# 3. 运行 mybot 并启用 OTel
MYBOT_OTEL_ENABLED=1 mybot

# 4. 打开 http://localhost:16686 → Search → Service: mybot → Find Traces
```

每条 trace 展示完整的调用树（`agent.run → llm.chat → tool.execute`），span 属性包括模型名称、token 消耗（`tokens_in` / `tokens_out` / `tokens_total`）、消息数量、工具名称和执行耗时。`OTelBridge`（`observability/otel_bridge.py`）将自定义 tracer 的 span 镜像到 OTel SDK 并通过 OTLP HTTP 导出——无需修改任何业务代码。

## 开发

```bash
ruff check .                               # lint
pytest                                     # 全部 961 个测试
pytest test/core/test_middleware.py -v     # 单个测试文件
pytest test/providers/test_openai_compatible_provider.py::TestParseDict::test_dict_with_choices -v
bash scripts/loc.sh                        # 按模块统计代码行数
```

## 评估系统

mybot 提供两层 Agent 评估体系：

**Layer 1 — 自定义任务（CI 就绪）**：9 个 YAML 定义的任务覆盖工具调用、推理、鲁棒性三类，
4 个规则评分器（完成率、关键词、工具集 Jaccard、步骤效率），pytest 集成。

```bash
pytest evals/ -v                             # CI 模式（mock，21 个测试）
python -m evals                              # 真实 LLM 评估（react）
python -m evals --paradigm react --paradigm plan_solve  # 范式对比
python -m evals --task file_read_basic       # 单任务
python -m evals -o report.md                 # 导出 Markdown 报告
```

**Layer 2 — 社区基准**：BFCL（函数调用准确率，AST 匹配）和 GAIA（通用 AI 能力，准精确匹配），
参考 hello-agents 的 Dataset→Evaluator→Metrics 三层架构。

```bash
# BFCL（需先 clone gorilla 仓库：git clone https://github.com/ShishirPatil/gorilla.git temp_gorilla）
python -m evals --benchmark bfcl --category simple_python --max-samples 20   # 单分类抽样
python -m evals --benchmark bfcl --category simple_python                     # 全量运行
# 可用分类: simple_python, simple_java, simple_javascript, multiple, parallel, irrelevance

# GAIA（需 HuggingFace token 访问 gaia-benchmark/GAIA，安装依赖: pip install huggingface-hub pyarrow）
python -m evals --benchmark gaia --level 1 --max-samples 10                  # 单级别抽样
python -m evals --benchmark gaia --level 1                                    # 全量运行
python -m evals --benchmark gaia                                              # 全部 3 个级别
```

## 路线图

### 已完成

- CLI 交互 UX 优化，Rich Live 流式渲染
- Prompt 模板统一管理（Jinja2）
- 子 Agent 委托（`SubAgentTool`）
- Provider API 错误处理、重试与恢复
- 工具系统安全边界（`ToolGuard`、作用域、能力检查）
- HTTP API + WebSocket + SSE 流式 + Web UI
- 可插拔中间件链（Agent / LLM / Tool 钩子）
- EventBus（异步发布/订阅）+ MessageBus（每通道独立出站队列）
- 22 个内置 Skill
- 上下文管理子系统（压缩、修复、空闲自动压缩）
- 基于文件的长期记忆系统（MemoryStore + Consolidator + Dream 两级管道）
- 会话历史持久化（基于游标的增量加载）
- 长任务断点恢复机制（checkpoint/resume）
- OpenTelemetry 桥接 → Jaeger trace 可视化
- MCP（Model Context Protocol）集成 — 连接外部工具服务器
- Memory Dream 系统 — 两阶段 LLM 记忆合并（Consolidator 实时摘要 + Dream 周期回顾）
- 混合搜索 + 时间衰减 — SQLite + sqlite-vec + FTS5，30 天半衰期指数衰减
- 可扩展频道架构 + 微信 iLink 机器人频道 — BaseChannel ABC，MessageBus 每通道路由
- 混合搜索可观测性 — 终端和 Web UI 日志视图显示搜索模式
- 多模态图像输入 — content-part 数组 + Web UI 拖拽粘贴 + 微信 ITEM_IMAGE
- Chunk 级检索 — 记忆按行/条目分块索引，向量 + FTS5 混合搜索

### P2 — 质量与可靠性

- ~~**Agent 性能评估系统（第一阶段）**~~ — 9 个自定义 YAML 任务 + 4 个规则评分器 + pytest CI 集成 ✅
- ~~**Agent 性能评估系统（第二阶段）**~~ — BFCL/GAIA 社区基准、LLM Judge 评分器、CLI 入口 ✅
- ~~**相关性筛选**~~ — 注入 MEMORY.md 前用混合搜索按当前 query 召回 top_k 相关条目 ✅

### P3 — 扩展能力

- ~~**多模态输入**~~ — 支持图片等非文本输入 ✅
- **更多 LLM Provider** — Anthropic 直连、Ollama 本地模型
- **更多消息频道** — QQ、Telegram、飞书等外部频道接入
- ~~**Heartbeat 服务**~~ — 周期任务检查，按 HEARTBEAT.md 清单定时执行（参考 nanobot）✅
- ~~**Skill 系统增强**~~ — Dream 自动提取重复工作流为可复用 SKILL.md ✅
- ~~**Chunk 级检索**~~ — 大文件分块存储和检索 ✅

## License

MIT
