<!-- Generated: 2026-07-13 | Files scanned: ~100 | Token estimate: ~450 -->

# Dependencies — External Services & Libraries

## Python Packages (`pyproject.toml`)

### Core
| Package | Purpose |
|---------|---------|
| `openai>=1.0` | LLM API client (OpenAI-compatible) |
| `httpx>=0.25` | HTTP client (iLink WeChat, web fetch) |
| `loguru>=0.7` | Structured logging |
| `rich>=13.0` | Terminal formatting |
| `textual>=8.0` | TUI framework (CLI mode) |
| `jinja2>=3.0` | Prompt template rendering |
| `pyyaml>=6.0` | YAML config parsing |
| `python-dotenv>=1.0` | .env loading |
| `json-repair>=0.1` | Repair malformed LLM JSON |
| `croniter>=2.0` | Cron expression parsing |
| `sentence-transformers>=3.0` | Embedding model for hybrid search |
| `sqlite-vec>=0.1` | Vector search in SQLite |

### Optional Extras
| Extra | Packages | Purpose |
|-------|----------|---------|
| `dev` | pytest, pytest-asyncio, ruff | Testing + lint |
| `server` | starlette, uvicorn | HTTP/WS server |
| `otel` | opentelemetry-api, opentelemetry-sdk, otlp-exporter | Trace export → Jaeger |
| `evals` | huggingface-hub, pyarrow | GAIA/BFCL benchmarks |

## External Services

| Service | Protocol | Purpose |
|---------|----------|---------|
| LLM API (OpenRouter/etc.) | HTTPS/SSE | LLM inference with streaming |
| iLink WeChat Bot (`ilinkai.weixin.qq.com`) | HTTPS long-poll | WeChat message bridge |
| Web Search (DuckDuckGo) | HTTPS | `websearch` tool backend |
| Jaeger (optional) | OTLP HTTP | Trace visualization |
| MCP servers (optional) | stdio/HTTP | External tool servers |

## File Dependencies

| Path | Purpose |
|------|---------|
| `~/.mybot/settings.json` | User config (env vars, model settings, thresholds) |
| `.env` | Fallback secrets |
| `scripts/xhs_cookies.json` | Xiaohongshu login cookie (Playwright) |
| `prompt_templates/` | 18 Jinja2 templates in subdirs: agent/, agent/deep_research/, agent/plan_solve/, dispatcher/, memory/ |
| `skills/` | 17 skill directories (algorithmic-art, brand-guidelines, canvas-design, doc-coauthoring, docx, frontend-design, internal-comms, mcp-builder, pdf, pptx, skill-creator, slack-gif-creator, theme-factory, web-artifacts-builder, webapp-testing, xiaohongshu, xlsx) |
| `utils/images.py` | ~56 lines — image file_to_data_url helper |

## Agent Discovery

```
agents/           → discover_agents() → {react, plan_solve, deep_research}
  team/           → NOT auto-discovered (sub-package with separate runner)
tools/            → discover_tools()  → bash, file R/W, grep, webfetch,
                    websearch, memory CRUD, subagent, schedule_task,
                    xiaohongshu_publish, + sandbox tools
```

## Test Stats
- 209 test functions in 39 files
- Framework: pytest + pytest-asyncio (asyncio_mode = "auto")
- Key test dirs: `test/core/`, `test/tools/`, `test/memory/`, `test/observability/`, `evals/`
