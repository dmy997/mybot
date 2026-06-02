# mybot

A multi-provider AI agent framework built for extensibility. Designed to work with any OpenAI-compatible API endpoint, with first-class support for OpenRouter, DeepSeek, and locally-hosted models.

> Early-stage project — core framework and provider abstractions are being built out.

## Architecture

```
mybot/
  core/           # Agent lifecycle, event system, skill orchestration
  providers/      # LLM backend abstraction layer
  agents/         # Agent definitions and configurations
  context/        # Conversation memory management
  skills/         # Pluggable agent skills
  tools/          # Tool definitions for LLM function calling
  observability/  # Logging, tracing, monitoring
```

### Key Abstractions

- **LLMProvider** (`providers/base.py`) — Abstract base class defining the chat interface. Providers implement `chat()` and optionally override `safe_chat_stream()` for streaming.
- **LLMResponse** — Unified response dataclass with content, tool calls, token usage, latency, and reasoning content (for thinking models like DeepSeek-R1).
- **OpenAICompatibleProvider** (`providers/openai_compatible_provider.py`) — Implementation for any OpenAI-compatible endpoint, with special handling for OpenRouter headers.

### Provider Features

| Feature | Status |
|---------|--------|
| OpenAI-compatible API support | In progress |
| OpenRouter integration | Planned |
| DeepSeek models | Planned |
| Local model support (Ollama/vLLM) | Planned |
| Streaming responses | Planned |
| Thinking/reasoning models | Planned |

## Installation

```bash
pip install -e .
```

For development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
import asyncio
from mybot.providers.openai_compatible_provider import OpenAICompatibleProvider

async def main():
    provider = OpenAICompatibleProvider(
        api_key="your-api-key",
        api_base="https://api.openai.com/v1",
        default_model="gpt-4o",
    )
    response = await provider.chat(
        messages=[{"role": "user", "content": "Hello!"}],
        tools=[],
    )
    print(response.content)

asyncio.run(main())
```

## Requirements

- Python 3.10+
- Dependencies: `openai`, `loguru`, `json-repair`

## License

MIT
