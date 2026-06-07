# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mybot is an early-stage multi-provider AI agent framework. It abstracts LLM backends behind a unified `LLMProvider` interface and is designed to support OpenAI-compatible APIs, OpenRouter, DeepSeek, and local models.

The only working module currently is `providers/`. The `core/`, `agents/`, `context/`, `tools/`, and `skills/` packages are empty stubs — they scaffold the planned architecture but contain no logic yet.

## Development Setup

```bash
pip install -e ".[dev]"
```

Copy `.env` and fill in your keys. The provider module reads `OPENAI_API_KEY`, `OPENAI_API_BASE`, `LLM_MODEL_ID`, and `PROVIDER_NAME` from the environment at runtime (uses `python-dotenv`).

## Build & Test

```bash
# Lint
ruff check .

# Run all tests
pytest

# Run a single test file
pytest test/providers/test_openai_compatible_provider.py

# Run a single test class or function
pytest test/providers/test_openai_compatible_provider.py::TestParseDict::test_dict_with_choices

# Verbose output
pytest -v
```

Tests use `pytest-asyncio` (asyncio_mode = "auto" in pyproject.toml). All LLM calls are async, so tests should use `@pytest.mark.asyncio` or rely on the auto-mode.

## Package Structure

- `providers/` — **The active module.** LLM backend abstraction (`LLMProvider` base class) + `OpenAICompatibleProvider` implementation.
- `core/` — Agent lifecycle, event system, runner, skill orchestration (all stubs)
- `agents/` — Agent definitions (stub)
- `context/` — Conversation memory/context management (stub)
- `tools/` — Tool definitions for LLM function calling (stub)
- `skills/` — Pluggable skills (stub)
- `observability/` — Logging, tracing, monitoring (stub)

## Key Abstractions

**`LLMProvider`** (`providers/base.py`) — Abstract base. Subclasses must implement `async def chat(messages, tools, model, max_tokens, temperature) -> LLMResponse`. The base provides `safe_chat()` with automatic error wrapping, and a default `safe_chat_stream()` that falls back to `chat()` for providers that don't support true streaming.

**`LLMResponse`** (`providers/base.py`) — Unified response dataclass: `content`, `tool_calls`, `usage`, `latency_s`, `reasoning_content` (for thinking models), `finish_reason`, `error`.

**`ToolCallRequest`** (`providers/base.py`) — Parsed tool call with `id`, `name`, `arguments`, plus `extra_content`, `provider_specific_fields`, and `function_provider_specific_fields` for provider-specific metadata.

**`OpenAICompatibleProvider`** (`providers/openai_compatible_provider.py`) — The main provider implementation. Key behaviors:

- **Two parsing paths**: `_parse()` handles complete responses (dict or Pydantic object), `_parse_chunks()` accumulates streaming chunks. Both handle malformed JSON via `json-repair`.
- **Streaming**: `chat_stream()` uses true SSE streaming with delta callbacks (`on_content_delta`, `on_thinking_delta`, `on_tool_call_delta`). Always sets `stream_options: {"include_usage": True}` for token counting.
- **OpenRouter auto-detection**: Sets `_default_headers` with `HTTP-Referer`, `X-OpenRouter-Title`, and `X-Session-Affinity` when `name="openrouter"` or the API base URL contains "openrouter".
- **Temperature gating**: Skips temperature for reasoning models in `_NO_SUP_TEMP_MODELS` (gpt-5, o1, o3, o4).
- **Token usage**: `_extract_usage()` probes three paths for cache-hit tokens: `prompt_tokens_details.cached_tokens`, `cached_tokens`, `prompt_cache_hit_tokens` — covering OpenAI, Anthropic, and OpenRouter conventions.
- **Lazy client init**: `AsyncOpenAI` client is built on first `chat()`/`chat_stream()` call, protected by an async lock.

## Code Conventions

- Python 3.10+ with modern typing (`list[dict]`, `str | None`, `dict[str, Any]`)
- Use `loguru` for logging (`from loguru import logger`)
- Use `dataclasses` for data containers
- Use `abc.ABC` / `@abstractmethod` for abstract interfaces
- Async-first: all LLM calls are `async`
- Provider implementations go in `providers/` and extend `LLMProvider`
- Imports within the package use relative paths: `from .base import LLMProvider`
- Use `json_repair.loads()` when parsing LLM-generated JSON (tool call arguments are frequently malformed)
- Module-level constants are prefixed with `_` (e.g., `_ALNUM`, `_NO_SUP_TEMP_MODELS`, `_STANDARD_TC_KEYS`)
