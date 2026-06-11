# mybot

A multi-provider AI agent framework with plugin-style agents, streaming output, long-term memory, and HTTP/WS API. Designed to work with any OpenAI-compatible API endpoint.

## Architecture

```
mybot/
  config/           # Config — .env auto-loading, typed configuration
  core/             # Orchestrator, Dispatcher, AgentCore, Middleware, EventBus, MessageBus
  agents/           # Paradigm agents (ReAct, PlanSolve) — auto-discovered
  context/          # ContextManager — session persistence, compression, repair
  providers/        # LLMProvider abstraction + OpenAI-compatible implementation
  tools/            # 10 tools (bash, file R/W, grep, webfetch, websearch, memory, subagent)
  memory/           # Long-term memory (file-based, LLM-callable CRUD)
  observability/    # Logging (loguru), metrics, tracing, CLI streaming (Rich Live)
  skills/           # 13 built-in skills (docx, pptx, pdf, canvas-design, etc.)
  utils/            # Jinja2 template rendering
  prompt_templates/ # 14 agent prompt templates
  server_web/       # Web chat UI
  test/             # 676 tests
```

### Request Flow

```
HTTP/WS → Orchestrator → Dispatcher → Paradigm Agent → AgentCore → LLMProvider
              ↑              ↑              ↑                ↑
          Middleware      路由决策      多轮编排       Middleware
          ContextManager                              (LLM/Tool)
          (会话/压缩/修复)
```

### Key Components

- **Orchestrator** (`core/orchestrator.py`) — 顶层协调层，交互式 CLI (prompt_toolkit + Rich Live) + HTTP API，管理请求全生命周期
- **MessageBus** (`core/message_bus.py`) — 双队列消息总线，解耦输入源（CLI/HTTP/WS）与输出消费者（流式渲染）
- **EventBus** (`core/events.py`) — 异步发布/订阅事件总线，isinstance 匹配，Agent 生命周期/LLM/Tool 事件通知
- **Server** (`core/server.py`) — Starlette HTTP API，SSE 流式 + WebSocket + Bearer 认证
- **Web UI** (`server_web/index.html`) — 浏览器聊天界面，流式渲染、Markdown、会话管理
- **Dispatcher** (`core/dispatcher.py`) — 四层路由：显式命令 → 关键词匹配 → LLM 分类 → 默认回退
- **AgentCore** (`core/runner.py`) — 共享的 agent 执行循环，流式 + 非流式，工具并行/串行执行，上下文压缩，LLM 错误恢复
- **Middleware** (`core/middleware.py`) — 可插拔的中间件链，拦截 LLM 调用、工具执行、agent 生命周期
- **ContextManager** (`context/context_manager.py`) — 会话管理、空闲压缩、token 预算压缩、中断修复
- **SkillsLoader** (`core/skills.py`) — 基于文件的 skill 发现（YAML 语法），自动注入系统 prompt
- **StreamRenderer** (`observability/stream_renderer.py`) — Rich Live 流式渲染器，支持原地更新 Markdown + ThinkingSpinner
- **Config** (`config/config.py`) — `.env` 自动加载，类型化配置

### Agent Paradigms

| Paradigm | 说明 |
|----------|------|
| `react` | 单轮推理+行动循环 |
| `plan_solve` | 先规划再执行，两阶段 |

Agents 通过 `agents/__init__.py` 的 `discover_agents()` 自动扫描发现。

## Installation

```bash
pip install -e ".[dev,server]"
```

Copy `.env` and fill in your keys:

```bash
cp .env.example .env
```

## Quick Start

```bash
# Interactive CLI
mybot

# HTTP/WS server
mybot-server
# Then open http://127.0.0.1:8080 in browser

# WebSocket (CLI client)
websocat ws://127.0.0.1:8080/ws/default
```

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

# Optional middleware
class AuditMiddleware(AgentMiddleware):
    async def on_tool_execute(self, ctx, call_next):
        print(f"tool: {ctx.tool_name}")
        return await call_next(ctx)

orche = Orchestrator(
    workspace=Config.workspace,
    provider=provider,
    compress_model=Config.light_model,
    middleware=MiddlewareChain([AuditMiddleware()]),
)

# Single message (streaming callbacks available)
result = await orche.process_message("default", "你好")
print(result.content)

# Interactive loop
await orche.run("default")
```

## Configuration

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENAI_API_KEY` | — | API 密钥 |
| `OPENAI_API_BASE` | — | API 地址 |
| `PROVIDER_NAME` | `openrouter` | provider 标识 |
| `LLM_MODEL_ID` | `deepseek/deepseek-v4-flash` | 默认模型 |
| `LIGHT_MODEL_NAME` | 同 `LLM_MODEL_ID` | 压缩/分类用小模型 |
| `LLM_TIMEOUT` | `60` | 请求超时(秒) |
| `WORKSPACE` | `~/.mybot/workspace` | 工作目录 |
| `MYBOT_API_KEY` | — | HTTP API Bearer 认证密钥（不设则不校验） |
| `MYBOT_HOST` | `127.0.0.1` | 服务绑定地址 |
| `MYBOT_PORT` | `8080` | 服务端口 |

## Development

```bash
# Lint
ruff check .

# Tests
pytest -q

# Single test file
pytest test/core/test_middleware.py -v
```

## Requirements

- Python 3.10+
- Dependencies: `openai`, `loguru`, `json-repair`, `rich`, `httpx`, `jinja2`, `python-dotenv`, `pyyaml`, `prompt_toolkit`
- Optional (server): `starlette`, `uvicorn`

## Roadmap / TODO

已完成的标记为 ✅，未完成的按功能完整性优先级排序。

1. ~~**命令行交互 UX 优化**~~ ✅
2. ~~**Prompt 模板统一管理**~~ ✅
3. ~~**子 Agent 模块**~~ ✅
4. ~~**Provider API 错误处理与重试**~~ ✅
5. ~~**工具系统安全边界**~~ ✅
6. ~~**P0 问题修复**~~ ✅ — `mybot`/`mybot-server` CLI 入口点，依赖声明补全
7. ~~**HTTP API + WebSocket + Web UI**~~ ✅ — SSE 流式、WebSocket 双向通信、Bearer 认证、浏览器聊天界面
8. ~~**中间件/Hook 系统**~~ ✅ — 可插拔中间件链，拦截 LLM 调用、工具执行、agent 生命周期，支持前置修改、后置修改、短路跳过

---

### P1 — Agent 系统核心能力缺口

9. ~~**内置 Skills**~~ ✅ — 13 个内置 skill（docx, pptx, pdf, xlsx, canvas-design, frontend-design, algorithmic-art, brand-guidelines, internal-comms, mcp-builder, skill-creator, slack-gif-creator, theme-factory, web-artifacts-builder, webapp-testing）
10. ~~**EventBus / MessageBus**~~ ✅ — `core/events.py` 异步发布/订阅事件总线，`core/message_bus.py` 双队列消息总线，解耦输入输出

### P1 — Agent 系统核心能力缺口（续）

11. **MCP（Model Context Protocol）集成** — 作为 MCP client 连接外部工具服务器，接入业界标准的工具生态

### P2 — 质量与可靠性

12. **智能体性能评估系统** — 建立 Agent 性能基准测试框架，包含标准任务集、自动化评测指标（任务完成率、步骤效率、工具选择准确率），支持回归测试和范式对比
13. **长任务断点恢复** — AgentCore 无检查点机制，长任务崩溃后无法续跑，需要状态快照和恢复能力
14. **Memory Dream 系统** — 利用空闲时间对历史会话进行回顾、总结和关联发现，将碎片记忆提炼为结构化知识

### P3 — 扩展能力

15. **多模态输入扩展** — 支持图片、音频等非文本输入，通过 provider 的多模态 API 传入 LLM
16. **更多 LLM Provider** — Anthropic 直连、Ollama 本地模型等
17. **多消息频道** — 微信、Telegram 等外部频道接入

## License

MIT
