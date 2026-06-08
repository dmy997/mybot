# mybot

A multi-provider AI agent framework with plugin-style agents, streaming output, and long-term memory. Designed to work with any OpenAI-compatible API endpoint.

## Architecture

```
mybot/
  config/         # Config — .env auto-loading, typed configuration
  core/           # Orchestrator, Dispatcher, AgentCore, SkillsLoader
  agents/         # Paradigm agents (ReAct, PlanSolve) — auto-discovered
  context/        # ContextManager — session persistence, compression, repair
  providers/      # LLMProvider abstraction + OpenAI-compatible implementation
  tools/          # Tool definitions + ToolRegistry
  memory/         # Long-term memory (file-based)
  utils/          # Template rendering
  prompt_templates/  # Agent prompt templates
  test/           # Test suite (307 tests)
```

### Request Flow

```
Orchestrator → Dispatcher → Paradigm Agent → AgentCore → LLMProvider
     ↑              ↑              ↑              ↑
  ContextManager  路由决策     多轮编排      工具调用循环
  (会话/压缩/修复)
```

### Key Components

- **Orchestrator** (`core/orchestrator.py`) — 顶层协调层，交互式循环读取 stdin，流式输出，管理请求全生命周期
- **Dispatcher** (`core/dispatcher.py`) — 四层路由：显式命令 → 关键词匹配 → LLM 分类 → 默认回退
- **AgentCore** (`core/runner.py`) — 共享的 agent 执行循环，支持流式 + 非流式，工具调用
- **ContextManager** (`context/context_manager.py`) — 会话管理、空闲压缩、token 预算压缩、中断修复
- **SkillsLoader** (`core/skills.py`) — 基于文件的 skill 发现，自动注入系统 prompt
- **Config** (`config/config.py`) — `.env` 自动加载，类型化配置

### Agent Paradigms

| Paradigm | 说明 |
|----------|------|
| `react` | 单轮推理+行动循环 |
| `plan_solve` | 先规划再执行，两阶段 |

Agents 通过 `agents/__init__.py` 的 `discover_agents()` 自动扫描发现。

## Installation

```bash
pip install -e ".[dev]"
```

Copy `.env` and fill in your keys:

```bash
cp .env.example .env
```

## Quick Start

```bash
# Interactive mode (streaming output)
python -m core.orchestrator
```

```python
import asyncio
from config import Config
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

# Single message
result = await orche._process_once("default", "你好")
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

## Development

```bash
# Lint
ruff check .

# Tests
pytest -q

# Single test file
pytest test/core/test_orchestrator.py -v
```

## Requirements

- Python 3.10+
- Dependencies: `openai`, `loguru`, `json-repair`, `python-dotenv`

## Roadmap / TODO

1. ~~**命令行交互 UX 优化**~~ ✅ — 屏蔽日志打印对交互模式的干扰，分离 stderr 日志输出与 stdout 用户交互内容，提升命令行使用体验
2. ~~**Prompt 模板统一管理**~~ ✅ — 将项目中所有 prompt 抽取为 `prompt_templates/` 下的模板文件，代码中只负责渲染模板变量，实现 prompt 与代码分离
3. ~~**子 Agent 模块**~~ ✅ — 实现 sub-agent 系统，支持主 Agent 将子任务委托给独立子 Agent 执行（如代码生成、搜索等专用子 Agent），子 Agent 拥有受限的工具集和独立的执行上下文
4. ~~**Provider API 错误处理与重试**~~ ✅ — 为 `LLMProvider` 的 API 调用添加指数退避重试、三级错误分类（retryable/recoverable/fatal）、context_length 自动压缩和 content_filter 合规提示恢复机制。所有 LLM 调用已统一通过 `chat_with_retry`/`chat_stream_with_retry` 进行
5. ~~**工具系统安全边界**~~ ✅ — 新增 `ToolGuard` 安全中间件（`tools/guard.py`），实现：Capability 能力声明机制、SSRF 内网地址阻断、命令注入增强检测、文件敏感路径保护、基于 scope 的网络/Shell 访问控制。子 Agent 默认禁止网络和 Shell 执行
6. **多消息频道 + MessageBus** — 添加微信、网页端等不同消息频道，通过消息总线 `MessageBus` 统一管理消息的生产与消费，实现频道接入与核心 Agent 的解耦
7. **MCP 模块** — 添加 Model Context Protocol 支持，使 mybot 能作为 MCP client 连接外部工具服务器，扩展工具生态
8. **Hook 钩子系统** — 在 Agent 生命周期的关键节点（请求前/后、工具调用前/后、LLM 调用前/后、异常退出等）提供可插拔的 hook 机制，支持用户自定义拦截、修改、审计逻辑
9. **定时服务 + Dream 记忆系统** — 添加定时任务调度模块，利用定时服务实现记忆系统 Dream 功能（定期对历史会话进行回顾、总结、关联发现）
10. **智能体性能评估系统** — 建立 Agent 性能基准测试框架，包含标准任务集、自动化评测指标（任务完成率、步骤效率、工具选择准确率、回复质量评分等），支持回归测试和范式对比
11. **多模态输入扩展** — 支持图片、音频、文件等多模态输入，通过 provider 的多模态 API 将非文本内容编码后传入 LLM，扩展 Agent 的感知能力

## License

MIT
