"""LLM Provider基类"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    ErrorCategory,
    LLMErrorInfo,
)
from .retry import RetryConfig, with_retry


@dataclass
class ToolCallRequest:
    """LLM返回的工具调用请求"""
    id: str
    name: str
    arguments: dict[str, Any]
    extra_content: dict[str, Any] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None


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
    latency_s: int = 0

    # LLM推理内容，仅限thinking model
    reasoning_content: str | None = None

    # response的原因：{"tool_calls", "function_call", "stop", "error"}
    finish_reason: str = "stop"

    # 调用出错时的异常信息
    error: dict[str, Any] | None = None


class LLMProvider(ABC):
    """LLM providers基类"""

    def __init__(self,
                 api_key: str | None = None,
                 api_base: str | None = None,
                 temperature: float = 0.7,
                 max_tokens: int = 4096,
                 ):
        self.api_key = api_key
        self.base_url = api_base
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

    # -- error classification -------------------------------------------------

    def _classify_error(self, error: Exception) -> LLMErrorInfo:
        """Categorise an exception raised during a chat call.

        Subclasses SHOULD override this to provide provider-specific
        classification (e.g. by inspecting OpenAI SDK exception types).
        The base implementation conservatively treats all errors as
        retryable.
        """
        return LLMErrorInfo(
            category=ErrorCategory.RETRYABLE,
            message=str(error) or type(error).__name__,
            error_type="unknown",
            raw_error=error,
        )

    # -- retry wrapper --------------------------------------------------------

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_config: RetryConfig | None = None,
    ) -> LLMResponse:
        """Call :meth:`chat` with automatic retry and error classification.

        Transient errors are retried with exponential backoff.  Permanent
        errors are raised as typed exceptions so the caller can distinguish
        retryable / recoverable / fatal.
        """
        return await with_retry(
            self.chat,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            classify_error=self._classify_error,
            config=retry_config,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> LLMResponse:
        """Call :meth:`chat_stream` with automatic retry and error classification."""

        async def _call() -> LLMResponse:
            return await self.chat_stream(
                messages=messages,
                tools=tools or [],
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                on_content_delta=on_content_delta,
                on_thinking_delta=on_thinking_delta,
                on_tool_call_delta=on_tool_call_delta,
            )

        return await with_retry(
            _call,
            classify_error=self._classify_error,
            config=retry_config,
        )

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
