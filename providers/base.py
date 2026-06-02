"""LLM Provider基类"""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from loguru import logger


@dataclass
class ToolCallRequest:
    """LLM返回的工具调用请求"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """LLM返回的结果"""

    # 返回内容
    content: str

    # 工具调用列表
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    
    # token用量统计
    usage: dict[str, int] = field(default_factory=dict)
    
    # 调用耗时
    latency_ms: int = 0

    # LLM推理内容，仅限thinking model
    reasoning_content: str | None = None

    # response的原因：{"tool_calls", "function_call", "stop", "error"}
    finish_reason: str = "stop"


class LLMProvider(ABC):
    """LLM providers基类"""
    
    def __init__(self, 
                 api_key: str | None = None,
                 api_base: str | None = None,
                 temperature: float = 0.7,
                 max_tokens: int = 4096,
                 ):
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens

    @abstractmethod
    async def chat(
        self, 
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None
    ) -> LLMResponse:
        """
        向LLM发送请求

        Args:
            messages: 消息列表, {"role": "user", "content": ""}
            tools: 可调用的工具列表
            model: 模型名
            max_tokens: 回复的最大token数
            temperature: LLM采样温度

        Returns:
            LLMResponse: 至少包含content或tool_calls
        """
        pass

    async def safe_chat(self, **kwargs: Any) -> LLMResponse:
        """安全调用LLM"""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def safe_chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """
        支持流式输出的providers应重写此方法
        """
        try:
            response = await self.chat(
                messages, 
                tools, 
                model, 
                max_tokens=max_tokens,
                temperature=temperature
            )
            if on_content_delta and response.content:
                await on_content_delta(response.content)
            return response
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return LLMResponse(content=f"Error calling LLM: {e}", finish_reason="error")
        