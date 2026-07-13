# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mybot is a multi-provider AI agent framework with plugin-style agents, streaming output, long-term memory, HTTP/WS API, and extensible chat channels (WeChat). Designed to work with any OpenAI-compatible API endpoint. 961 tests, all passing.

## Development Setup

```bash
pip install -e ".[dev,server]"
cp .env.example .env   # then fill in your keys
```

## Build & Test

```bash
ruff check .           # lint
pytest                 # all 961 tests (pytest-asyncio, asyncio_mode = "auto")
pytest test/core/test_middleware.py -v   # single file
pytest test/providers/test_openai_compatible_provider.py::TestParseDict::test_dict_with_choices -v
```

## Running

```bash
mybot                  # interactive CLI (core.orchestrator:main)
mybot-server           # HTTP/WS server (core.server:main), then open http://127.0.0.1:8080
```

## Package Layout (flat, no `mybot/` subdirectory)

- `providers/` — LLM backend abstraction (`LLMProvider` base, `OpenAICompatibleProvider`, retry logic, error types, factory)
- `core/` — Orchestrator (composed from 4 mixins: MCPServices, ToolRegistry, SessionLifecycle, IdleCompression), Dispatcher, AgentCore (runner), Agent base class, Middleware chain, SkillsLoader, HTTP/WS server
- `agents/` — ReAct Agent (single-pass) + PlanSolve Agent (two-phase). Auto-discovered via `discover_agents()`
- `evals/` — Agent evaluation system (custom YAML tasks + BFCL/GAIA benchmarks)
- `context/` — ContextManager composed from 4 mixins: CoreContextMixin (build_messages, compress), PromptBuilderMixin (system prompt, repair), MemoryOperationsMixin (remember/forget/recall), SessionPersistenceMixin (save/list/delete/expire). + SessionManager (JSON persistence with hard-cap pruning + TTL expiry)
- `tools/` — bash, file R/W, grep, webfetch, websearch, memory CRUD, subagent, `schedule_task` (create/list/cancel periodic tasks), ToolRegistry, ToolGuard security
- `services/` — `CronScheduler` (self-driven timer, cron-expression + interval jobs) + `ScheduledTaskService` (chat-created push tasks + system side-effect tasks like Xiaohongshu) + `HitlService` / `HitlMiddleware` (human-in-the-loop confirmation). See `docs/scheduled-tasks.md` and `docs/hitl.md`
- `memory/` — Long-term file-based memory (MemoryStore, Consolidator + Dream pipeline)
- `observability/` — Structured logging (loguru), Prometheus-style metrics, span tracing, rich CLI display
- `config/` — `.env` auto-loading, typed `Config` class
- `utils/` — Jinja2 template rendering (`render_template()`)
- `prompt_templates/` — 14 agent prompt templates (Jinja2 `.md`)
- `skills/` — Empty (only `__init__.py`). SkillsLoader works but has no skill directories to load
- `server_web/` — Single `index.html` for the browser chat UI

## Request Flow

```
HTTP/WS or CLI → Orchestrator → ContextManager.build_messages()
                                   ├─ repair interrupted session
                                   ├─ assemble system prompt (base + memory + skills + tools + history summaries)
                                   ├─ load session history (cursor-based, 100-msg cap)
                                   └─ token-budget check → compress if needed
                → Dispatcher.resolve()
                    ├─ Layer 1: explicit commands (/react, /plan)
                    ├─ Layer 2: keyword heuristics (multi-step → plan_solve)
                    ├─ Layer 3: LLM classification (optional, cheap model)
                    └─ Layer 4: default (react)
                → Agent.run(AgentInput) → AgentCore.run()
                    └─ loop: LLM call → tool calls (parallel + serial) → feed results back
                → ContextManager.save_exchange() → persist to disk
```

## Key Abstractions & Patterns

**`LLMProvider` → `OpenAICompatibleProvider`** (`providers/base.py`, `providers/openai_compatible_provider.py`):
- Abstract base: `async chat(messages, tools, model, max_tokens, temperature) -> LLMResponse`
- Also provides `safe_chat()`, `chat_with_retry()`, `chat_stream()` (true SSE with delta callbacks), `chat_stream_with_retry()`
- Lazy `AsyncOpenAI` client init protected by async lock
- OpenRouter auto-detection: sets referer/session-affinity headers when `name="openrouter"` or URL contains "openrouter"

**`AgentCore`** (`core/runner.py`): The shared execution loop used by ALL agent paradigms. Calls LLM in a loop, executes tool calls, feeds results back. Handles:
- Parallel vs serial tool execution (`asyncio.gather` for parallel-safe tools)
- Context compaction (7-step pipeline on copies: remove orphans → fill missing → summarize old → truncate long → token budget → repair)
- LLM error recovery (context_length → compact & retry; content_filter → append compliance hint & retry)
- Stall detection at 50+ steps
- Middleware hooks at agent start/step/end, LLM call, tool execution

**`BaseAgent`** (`core/agent_base.py`): ABC for paradigm agents. Each subclass implements `run(AgentInput) -> AgentOutput`. Paradigm agents only describe *what* messages to send — they never touch LLM APIs directly.

**`AgentInput` / `AgentOutput`** (`core/runner.py`): The universal I/O contract. `AgentInput` carries messages + tools + streaming callbacks (`on_content_delta`, `on_thinking_delta`, `on_tool_call_delta`, `on_tool_execute_start/end`, `on_new_turn`). `AgentOutput` carries full message history so callers can continue the conversation.

**Middleware** (`core/middleware.py`): Chain-of-responsibility pattern. `MiddlewareChain` nests `call_next` closures so middleware[n] wraps middleware[n+1]. Five hook points: `on_agent_start`, `on_agent_step` (can abort loop), `on_llm_call` (modify messages/model before, inspect response after), `on_tool_execute` (block/modify/cache results), `on_agent_end`. Shared mutable state via `MiddlewareContext.data`.

**HITL** (`services/hitl.py`): Human-in-the-loop tool authorization. `HitlMiddleware` (AgentMiddleware) pauses tool execution in `on_tool_execute` when `HITL_MODE=confirm` and the tool has SHELL/FILE_WRITE/NETWORK/DELEGATE capabilities. `HitlService` uses `asyncio.Future` to block until the user approves/denies via channel-specific UI (CLI confirm dialog, HTTP `/hitl/respond`, WeChat y/n reply). Default mode is `bypass` (auto-execute all).

**`Dispatcher`** (`core/dispatcher.py`): Four-layer routing (regex commands → keyword heuristics → optional LLM classification → default). The LLM classifier is instantiated internally when `provider` is given — uses a cheap model for <10 token responses.

**`ContextManager`** (`context/context_manager.py` + `context/context_manager_mixins.py`): Unified context assembly via 4 mixins. Key behaviors:
- Compression is **non-destructive**: `session.messages` is never modified. `CompactionService` advances `consolidated_cursor` to skip old messages; `Consolidator` appends LLM summaries to `memory/history.jsonl`
- System prompt assembly: base prompt → skills → tools → memory context (SOUL.md, USER.md, MEMORY.md) → file context → recent history
- Session repair on load: detects unmatched tool calls, missing assistant responses (3 interruption patterns)
- Idle compression + token-budget compression share the same `compress()` method
- Session hard cap (`prune_by_count`, default 2000 msgs) decoupled from consolidation
- Session TTL expiry (`purge_expired_sessions`, default 30 days) runs hourly in serve loop

**`Tool` / `ToolRegistry` / `ToolGuard`** (`tools/`):
- Every tool extends `Tool` ABC: set `name`, `description`, `parameters` (JSON Schema), `capabilities`, `_scopes`, `_parallel`
- `ToolGuard` maps capabilities to security checks: SHELL → injection detection, NETWORK → SSRF check, FILE_READ/WRITE → sensitive path blocklist
- Tools are auto-discovered by `discover_tools()`, scanning for `Tool` subclasses

**Orchestrator mixins** (`core/orchestrator_mixins.py`): Decomposed into MCPServicesMixin (MCP + cron + scheduled tasks), ToolRegistryMixin (tool discovery/registration), SessionLifecycleMixin (session CRUD + memory + dispatcher), and IdleCompressionMixin (idle compression + session expiry).

**Auto-discovery**: Both agents (`agents/discover_agents()`) and tools (`tools/discover_tools()`) use `pkgutil.iter_modules` + `inspect.getmembers` to find subclasses at import time. New agents/tools are picked up automatically.

## Code Conventions

- Python 3.10+ with modern typing (`list[dict]`, `str | None`, `dict[str, Any]`)
- `loguru` for logging (`from loguru import logger`)
- `dataclasses` for data containers; `abc.ABC` / `@abstractmethod` for interfaces
- Async-first: all LLM calls and tool executions are `async`
- Package imports use relative paths: `from .base import LLMProvider`
- `json_repair.loads()` for parsing LLM-generated JSON (arguments frequently malformed)
- Module-level constants prefixed with `_` (e.g., `_NO_SUP_TEMP_MODELS`, `_DEFAULT_MAX_ITERATIONS`)
- Prompt templates in `prompt_templates/`, loaded via `utils.render_template(name, **vars)`
- Templates ending in `.md` are rendered as Jinja2 with `strip=True`

## Configuration

All settings live in `~/.mybot/settings.json` under the `"env"` key (JSON, follows Claude Code's `~/.claude/settings.json` pattern). First run auto-generates it with defaults. Priority: shell env > settings.json > `.env` file.

### Environment Variables (in `settings.json` → `"env"`)

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
| `HYBRID_SEARCH_ENABLED` | `true` | Enable SQLite FTS5 + sqlite-vec hybrid search |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers embedding model |
| `CONTEXT_WINDOW` | `200000` | Default context window in tokens |
| `MAX_OUTPUT_TOKENS` | `20000` | Tokens reserved for model output |
| `WARNING_BUFFER_RATIO` | `0.11` | Fraction of effective_window for warning threshold |
| `AUTOCOMPACT_BUFFER_RATIO` | `0.072` | Fraction of effective_window for auto-compact threshold |
| `BLOCK_BUFFER_RATIO` | `0.017` | Fraction of effective_window for block threshold |
| `COMPRESS_RATIO` | `0.5` | Fraction of context window for recent messages during compression |
| `CONSOLIDATION_RATIO` | `0.7` | Fraction triggering background consolidation |
| `IDLE_COMPRESS_SECONDS` | `300` | Seconds of inactivity before idle compression (0=disabled) |
| `MAX_SESSION_MESSAGES` | `2000` | Hard cap on session message count — oldest pruned first |
| `SESSION_TTL_DAYS` | `30` | Days of inactivity before session auto-deletion (0=disabled) |

## Settings File (`~/.mybot/settings.json`)

Per-model context window configuration (JSON, follows Claude Code's `~/.claude/settings.json` pattern):

```json
{
  "env": {
    "PROVIDER_NAME": "openrouter",
    "LLM_MODEL_ID": "deepseek/deepseek-v4-flash",
    "OPENAI_API_KEY": "sk-or-v1-...",
    "OPENAI_API_BASE": "https://openrouter.ai/api/v1",
    ...
  },
  "models": [
    {"pattern": "deepseek/*", "context_window": 200000, "max_output_tokens": 20000},
    {"pattern": "gpt-4o*",   "context_window": 128000, "max_output_tokens": 16384},
    {"pattern": "claude-*",  "context_window": 200000, "max_output_tokens": 32000},
    {"pattern": "*",         "context_window": 200000, "max_output_tokens": 20000}
  ],
  "thresholds": {
    "warning_buffer_ratio": 0.11,
    "auto_compact_buffer_ratio": 0.072,
    "block_buffer_ratio": 0.017,
    "compress_ratio": 0.5,
    "consolidation_ratio": 0.7,
    "idle_compress_seconds": 300,
    "max_session_messages": 2000,
    "session_ttl_days": 30
  }
}
```

- `models[].pattern` — fnmatch pattern, first-match-wins, `"*"` as catch-all default
- `thresholds` — all optional, fall back to env vars then hardcoded defaults
- Auto-generated with defaults on first run if missing
- Priority: settings.json > env var > hardcoded default

## Known Gaps (from README.md roadmap)

- **P2**: ~~Hybrid search (SQLite + sqlite-vec + FTS5), temporal decay~~ (done)
- **P3**: Multimodal input, more providers (Anthropic direct, Ollama), external chat channels, Heartbeat service, Skill system enhancement, chunk-level retrieval
- See `README.md` Roadmap for the full prioritized list with status markers

## Memory System Improvement Roadmap

See `docs/memory-comparison.md` for the full cross-project analysis. Summary:

- **P1 (short-term)**: ~~Dream dedup~~, ~~age annotations (`<- Nd`)~~, ~~session source tracking in history.jsonl~~
- **P2 (medium-term)**: ~~Hybrid search (SQLite + sqlite-vec + FTS5), temporal decay~~ (done)
- **P3 (long-term)**: Heartbeat service, Skill system, chunk-level retrieval granularity
