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

## License

MIT
