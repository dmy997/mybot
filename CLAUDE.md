# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

mybot is an early-stage multi-provider AI agent framework. It abstracts LLM backends behind a unified `LLMProvider` interface and is designed to support OpenAI-compatible APIs, OpenRouter, DeepSeek, and local models.

## Package Structure

- `providers/` — LLM backend abstraction (`LLMProvider` base class + implementations)
- `core/` — Agent lifecycle, event system, runner, skill orchestration
- `agents/` — Agent definitions
- `context/` — Conversation memory/context management
- `tools/` — Tool definitions for LLM function calling
- `skills/` — Pluggable skills
- `observability/` — Logging, tracing, monitoring

## Development Setup

```bash
pip install -e ".[dev]"
```

## Code Conventions

- Python 3.10+ with modern typing (`list[dict]`, `str | None`, etc.)
- Use `loguru` for logging (`from loguru import logger`)
- Use `dataclasses` for data containers
- Use `abc.ABC` / `@abstractmethod` for abstract interfaces
- Async-first: all LLM calls are `async`
- Provider implementations go in `providers/` and extend `LLMProvider`
- Imports use the full package path: `from mybot.providers.base import LLMProvider`

## Key Files

- `providers/base.py` — `LLMProvider` abstract base, `LLMResponse`, `ToolCallRequest`
- `providers/openai_compatible_provider.py` — `OpenAICompatibleProvider` (in progress)
- `providers/factory.py` — Provider factory (planned)

## Build & Test

```bash
# Lint
ruff check .

# Run tests
pytest
```
