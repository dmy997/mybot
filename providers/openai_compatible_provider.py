"""OpenAI兼容接口"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import string
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import json_repair
from loguru import logger
from openai import AsyncOpenAI

from mybot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI兼容API的Provider
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        name: str | None = None,
        default_model: str = "deepseek-chat",
        is_local: bool = False
    ):
        super().__init__(api_key, api_base)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.name = name
        self.is_local = is_local
        if name == "openrouter" or \
            bool(api_base and "openrouter" in api_base.lower()):
            self._default_headers = {
                "x-session-affinity": uuid.uuid4().hex,
                "HTTP-Referer": "https://github.com/dmy997/mybot",
                "X-OpenRouter-Title": "mybot",
                "X-OpenRouter-Categories": "cli-agent,personal-agent",
            }
        
        self._client = AsyncOpenAI | None = None
        self._client_lock = asyncio.Lock()
