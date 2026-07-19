# mybot

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1037%20passed-green)](.)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

A personal AI assistant framework inspired by **Claude Code**, **nanobot**, and **OpenClaw**, built with Claude Code vibe coding. Highly readable, modular, and lightweight.

**English** | [中文](./README.md)

## Highlights

- **Multi-provider** — any OpenAI-compatible API endpoint (OpenRouter, DeepSeek, local models)
- **Paradigm agents** — ReAct, Plan-and-Solve, DeepResearch (orchestrator-workers); auto-discovered via `discover_agents()`
- **Streaming** — SSE, WebSocket, and Rich terminal UI with live tool-use rendering
- **Pluggable middleware** — chain-of-responsibility hooks for LLM calls, tool execution, agent lifecycle
- **Long-term memory** — file-based typed memory (user / feedback / project / reference) with hybrid search (vector + FTS5)
- **Context management** — non-destructive compression, session repair, idle auto-compaction
- **Semantic filtering** — embedding-based tool/skill ranking via cosine similarity, top-k dynamic selection per query (P1)
- **Reflect mode** — post-generation self-review pass without tools, checking factuality, logic, coverage, and clarity
- **HITL** — human-in-the-loop confirmation for high-risk tools (SHELL/FILE_WRITE/NETWORK/DELEGATE)
- **Observability** — structured logging (loguru), custom metrics/tracing, and optional OpenTelemetry → Jaeger bridge
- **Checkpoint/resume** — crash recovery for long-running agent tasks
- **17 built-in skills** — docx, pptx, pdf, xlsx, canvas-design, frontend-design, algorithmic-art, brand-guidelines, internal-comms, mcp-builder, skill-creator, slack-gif-creator, theme-factory, web-artifacts-builder, webapp-testing, xiaohongshu, heartbeat

## Architecture

### Request Flow

```
HTTP/WS or CLI → Orchestrator → ContextManager.build_messages()
                                   ├─ repair interrupted session
                                   ├─ assemble system prompt
                                   ├─ load session history
                                   └─ token-budget check → compress if needed
                → Dispatcher.resolve()
                    ├─ Layer 1: explicit commands (/react, /plan, /research)
                    ├─ Layer 2: keyword heuristics
                    ├─ Layer 3: LLM classification (optional)
                    └─ Layer 4: default (react)
                → Agent.run(AgentInput) → AgentCore.run()
                    └─ loop: LLM call → tool calls (parallel + serial) → feed results back
                    └─ optional: reflect pass (/reflect prefix or REFLECT_ENABLED)
                → ContextManager.save_exchange() → persist to disk
```

### Key Components

| Component | File | Role |
|-----------|------|------|
| Orchestrator | `core/orchestrator.py` | Top-level coordinator — CLI loop, HTTP API, request lifecycle |
| Dispatcher | `core/dispatcher.py` | Four-layer routing: commands → heuristics → LLM classify → default |
| AgentCore | `core/runner.py` | Shared execution loop — streaming, tool exec, compaction, error recovery |
| Middleware | `core/middleware.py` | Pluggable chain — intercepts LLM calls, tool exec, agent lifecycle |
| EventBus | `core/events.py` | Async pub/sub — Agent/LLM/Tool lifecycle events |
| MessageBus | `core/message_bus.py` | Dual-queue bus — decouples input sources from output consumers |
| ContextManager | `context/context_manager.py` | Session persistence, idle compaction, token-budget compression, interruption repair, semantic filtering |
| MemoryStore | `memory/store.py` | File I/O for typed long-term memories |
| StreamRenderer | `observability/stream_renderer.py` | Rich Live terminal streaming with Markdown + ThinkingSpinner |
| SkillsLoader | `core/skills.py` | File-based skill discovery (YAML), keyword triggers + semantic similarity injection |

### Agent Paradigms

| Paradigm | Description |
|----------|-------------|
| `react` | Single-pass reasoning + action loop |
| `plan_solve` | Plan first, then execute — two-phase |
| `deep_research` | Orchestrator-workers: decompose → parallel search → synthesis |

## Installation

```bash
pip install -e ".[dev,server]"
cp .env.example .env   # then fill in your API keys
```

Optional dependencies:

```bash
pip install -e ".[otel]"   # OpenTelemetry → Jaeger bridge
```

## Quick Start

```bash
# Interactive CLI
mybot

# HTTP/WS server
mybot-server
# Then open http://127.0.0.1:8080 in browser

# WebSocket
websocat ws://127.0.0.1:8080/ws/default
```

### Programmatic Usage

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

# Single message
result = await orche.process_message("default", "你好")
print(result.content)

# Interactive loop
await orche.run("default")
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | — | API key |
| `OPENAI_API_BASE` | — | API base URL |
| `PROVIDER_NAME` | `openrouter` | Provider identifier |
| `LLM_MODEL_ID` | `deepseek/deepseek-v4-flash` | Default model |
| `LIGHT_MODEL_NAME` | same as above | Cheap model for compression/classification |
| `LLM_TIMEOUT` | `60` | Request timeout (seconds) |
| `WORKSPACE` | `~/.mybot/workspace` | Sessions + memory storage |
| `MYBOT_API_KEY` | — | Bearer auth for HTTP/WS (disabled when unset) |
| `MYBOT_HOST` | `127.0.0.1` | Server bind address |
| `MYBOT_PORT` | `8080` | Server port |
| `MYBOT_CHECKPOINT` | — | Enable checkpoint/resume for long tasks |
| `REFLECT_ENABLED` | `false` | Enable reflect mode globally |
| `REFLECT_MODEL` | — | Model override for reflection (empty = main model) |
| `REFLECT_PROMPT` | — | Custom reflection review prompt |
| `HITL_MODE` | `bypass` | Human-in-the-loop: `bypass` (auto) or `confirm` (require approval) |
| `HYBRID_SEARCH_ENABLED` | `true` | Enable SQLite FTS5 + sqlite-vec hybrid search |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers embedding model |
| `MYBOT_OTEL_ENABLED` | — | Enable OpenTelemetry bridge |
| `MYBOT_OTEL_ENDPOINT` | `http://localhost:4318/v1/traces` | OTLP HTTP endpoint |

## Observability

mybot provides two complementary observability approaches.

### 1. Built-in Observability (zero-dependency)

Works out of the box with no external services:

- **Structured logging** (`observability/log.py`) — loguru-based, all events carry typed fields (`event_type`, `trace_id`, `span_id`, `latency_ms`)
- **Metrics** (`observability/metrics.py`) — in-memory `Counter` / `Gauge` / `Histogram` with a global `REGISTRY` singleton; snapshot via `REGISTRY.collect_all()`
- **Tracing** (`observability/trace.py`) — `contextvars`-based span propagation, `with tracer.span("llm.chat", model="gpt-4"): ...`, emitted as structured log events
- **Event bus subscribers** (`observability/subscribers.py`) — bridge agent/LLM/tool lifecycle events to metrics and logs

### 2. Visual Dashboard (OpenTelemetry → Jaeger)

Enable a full trace visualization pipeline with one environment variable:

```bash
# 1. Install OTel dependencies
pip install "mybot[otel]"

# 2. Start Jaeger (one docker command)
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one

# 3. Run mybot with OTel enabled
MYBOT_OTEL_ENABLED=1 mybot

# 4. Open http://localhost:16686 → Search → Service: mybot → Find Traces
```

Each trace shows the full call tree (`agent.run → llm.chat → tool.execute`) with span attributes including model name, token counts (`tokens_in` / `tokens_out` / `tokens_total`), message count, and tool names. The `OTelBridge` (`observability/otel_bridge.py`) mirrors custom tracer spans to the OTel SDK and exports via OTLP HTTP — no changes to business code required.

## Development

```bash
ruff check .                               # lint
pytest                                     # all 1037 tests
pytest test/core/test_middleware.py -v     # single file
pytest test/providers/test_openai_compatible_provider.py::TestParseDict::test_dict_with_choices -v
bash scripts/loc.sh                        # line count by module 
```

## Evaluation System

mybot provides a two-layer agent evaluation framework:

**Layer 1 — Custom Tasks (CI-ready)**: 9 YAML-defined tasks covering tool usage, reasoning, and robustness,
with 4 rule-based scorers (completion rate, keyword match, tool Jaccard, step efficiency), integrated with pytest.

```bash
pytest evals/ -v                             # CI mode (mock, 21 tests)
python -m evals                              # Live LLM evaluation (react)
python -m evals --paradigm react --paradigm plan_solve  # Paradigm comparison
python -m evals --task file_read_basic       # Single task
python -m evals -o report.md                 # Export Markdown report
```

**Layer 2 — Community Benchmarks**: BFCL (function-calling accuracy via AST matching) and GAIA (general AI capability via quasi-exact match),
following hello-agents' Dataset→Evaluator→Metrics architecture.

```bash
# BFCL (clone gorilla repo first: git clone https://github.com/ShishirPatil/gorilla.git temp_gorilla)
python -m evals --benchmark bfcl --category simple_python --max-samples 20
python -m evals --benchmark bfcl --category simple_python

# GAIA (requires HuggingFace token for gaia-benchmark/GAIA, install: pip install huggingface-hub pyarrow)
python -m evals --benchmark gaia --level 1 --max-samples 10
python -m evals --benchmark gaia --level 1
python -m evals --benchmark gaia
```

## Roadmap

### Completed

- CLI UX overhaul with Rich Live streaming
- Unified prompt templates (Jinja2)
- Sub-agent delegation (`SubAgentTool`)
- Provider API error handling with retry and recovery
- Tool security boundary (`ToolGuard`, scopes, capability checks)
- HTTP API + WebSocket + SSE streaming + Web UI
- Pluggable middleware chain (agent / LLM / tool hooks)
- EventBus (async pub/sub) + MessageBus (dual-queue I/O)
- 17 built-in skills
- Context management subsystem (compression, repair, idle auto-compaction)
- Long-term file-based memory system (store–manager–service, typed entries)
- Session history persistence with cursor-based loading
- Checkpoint/resume for long-running agent tasks
- OpenTelemetry bridge → Jaeger trace visualization
- MCP (Model Context Protocol) integration — connect to external tool servers
- Memory Dream system — two-stage LLM memory consolidation (Consolidator + Dream)
- Hybrid search + temporal decay — SQLite + sqlite-vec + FTS5, 30-day half-life decay
- Extensible channel architecture + WeChat iLink bot — BaseChannel ABC, MessageBus routing
- Hybrid search observability — terminal and Web UI log views showing search patterns
- Multimodal image input — content-part arrays + Web UI drag/paste + WeChat ITEM_IMAGE
- Chunk-level retrieval — per-line/entry chunk indexing, vector + FTS5 hybrid search
- Reflect mode — post-generation tool-less self-review (/reflect prefix or REFLECT_ENABLED)
- HITL — human-in-the-loop confirmation for high-risk tools (HITL_MODE=confirm)
- DeepResearch Agent — orchestrator-workers: decompose → parallel search → synthesis
- Agent evaluation system — 9 custom YAML tasks + 4 rule scorers + BFCL/GAIA benchmarks
- Heartbeat service — periodic task checking per HEARTBEAT.md checklist
- Skill system enhancement — Dream auto-extraction of workflows into reusable SKILL.md

### P2 — Quality & Reliability

- **Dynamic semantic filtering** — enable tool filtering by default once optimal top-k validated
- **Reflect mode improvements** — Web UI before/after comparison, reflect quality scoring

### P3 — Extensibility

- **Additional LLM providers** — Anthropic direct, Ollama local models
- **More chat channels** — QQ, Telegram, Discord integration

## License

MIT
