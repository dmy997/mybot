"""OpenAI兼容接口"""

from __future__ import annotations

import asyncio
import os
import secrets
import string
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import json_repair
from loguru import logger
from openai import AsyncOpenAI

from .base import LLMProvider, LLMResponse, ToolCallRequest
from .errors import LLMErrorInfo

_OPENAI_TIMEOUT_S = 120.0
_NO_SUP_TEMP_MODELS = ("gpt-5", "o1", "o3", "o4")
_STANDARD_TC_KEYS = frozenset({"id", "type", "index", "function"})
_STANDARD_FN_KEYS = frozenset({"name", "arguments"})
_ALNUM = string.ascii_letters + string.digits


def _short_tool_id() -> str:
    """9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


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
        default_model: str = "deepseek/deepseek-v4-flash",
        is_local: bool = False
    ):
        super().__init__(api_key, api_base)
        self._default_model = default_model
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

        self._client: AsyncOpenAI | None = None
        self._client_lock = asyncio.Lock()

    def _build_client(self):
        time_out_s = os.environ.get("_OPENAI_TIMEOUT_S", _OPENAI_TIMEOUT_S)

        http_client: httpx.AsyncClient | None = None

        if self.is_local:
            http_client = httpx.AsyncClient(
                timeout=time_out_s,
                limits=httpx.Limits(keepalive_expiry=0)
            )

        # Note: SDK-level max_retries is set to 0 so our own retry layer
        # has full control.  The SDK retry is opaque and doesn't distinguish
        # error categories.
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http_client,
            default_headers=self._default_headers,
            max_retries=0,
            timeout=time_out_s
        )

    # -- error classification -------------------------------------------------

    def _classify_error(self, error: Exception) -> LLMErrorInfo:
        """Categorise OpenAI SDK exceptions.

        Retryable: rate-limit, server errors, network issues.
        Recoverable: context too long, content filter.
        Fatal: auth failures, bad requests, model not found.
        """
        from .errors import ErrorCategory, LLMErrorInfo

        # Attempt to import OpenAI SDK exception types
        try:
            import openai
            _openai_available = True
        except ImportError:
            _openai_available = False

        # -- Retryable: rate limiting -----------------------------------------
        if _openai_available and isinstance(error, openai.RateLimitError):
            retry_after: float | None = None
            body = getattr(error, "response", None)
            if body is not None:
                headers = getattr(body, "headers", {}) or {}
                raw = headers.get("retry-after") or headers.get("Retry-After")
                if raw is not None:
                    try:
                        retry_after = float(raw)
                    except (ValueError, TypeError):
                        pass
            return LLMErrorInfo(
                category=ErrorCategory.RETRYABLE,
                message=str(error),
                status_code=429,
                error_type="rate_limit",
                retry_after=retry_after,
                raw_error=error,
            )

        # -- Retryable: server errors -----------------------------------------
        if _openai_available and isinstance(error, openai.InternalServerError):
            status = getattr(error, "status_code", None)
            return LLMErrorInfo(
                category=ErrorCategory.RETRYABLE,
                message=str(error),
                status_code=status or 500,
                error_type="server_error",
                raw_error=error,
            )

        # -- Retryable: connection / timeout ----------------------------------
        if _openai_available and isinstance(error, (
            openai.APIConnectionError,
            openai.APITimeoutError,
        )):
            return LLMErrorInfo(
                category=ErrorCategory.RETRYABLE,
                message=str(error),
                error_type="network_error",
                raw_error=error,
            )

        # -- Fatal: authentication / permissions ------------------------------
        if _openai_available and isinstance(error, openai.AuthenticationError):
            return LLMErrorInfo(
                category=ErrorCategory.FATAL,
                message=str(error),
                status_code=getattr(error, "status_code", 401),
                error_type="auth_error",
                raw_error=error,
            )

        if _openai_available and isinstance(error, openai.PermissionDeniedError):
            return LLMErrorInfo(
                category=ErrorCategory.FATAL,
                message=str(error),
                status_code=getattr(error, "status_code", 403),
                error_type="permission_denied",
                raw_error=error,
            )

        # -- Bad request — inspect message for recoverable subtypes -----------
        if _openai_available and isinstance(error, openai.BadRequestError):
            msg = str(error).lower()
            if "context_length" in msg or "maximum context" in msg or "token" in msg:
                return LLMErrorInfo(
                    category=ErrorCategory.RECOVERABLE,
                    message=str(error),
                    status_code=400,
                    error_type="context_length",
                    raw_error=error,
                )
            if "content_filter" in msg or "content policy" in msg or "safety" in msg:
                return LLMErrorInfo(
                    category=ErrorCategory.RECOVERABLE,
                    message=str(error),
                    status_code=400,
                    error_type="content_filter",
                    raw_error=error,
                )
            return LLMErrorInfo(
                category=ErrorCategory.FATAL,
                message=str(error),
                status_code=400,
                error_type="bad_request",
                raw_error=error,
            )

        # -- Fatal: not found -------------------------------------------------
        if _openai_available and isinstance(error, openai.NotFoundError):
            return LLMErrorInfo(
                category=ErrorCategory.FATAL,
                message=str(error),
                status_code=404,
                error_type="not_found",
                raw_error=error,
            )

        # -- Fallback: treat unknown errors as retryable ----------------------
        return LLMErrorInfo(
            category=ErrorCategory.RETRYABLE,
            message=str(error) or type(error).__name__,
            error_type="unknown",
            raw_error=error,
        )

    def _build_chat_completion_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        model = model or self._default_model
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        if model not in _NO_SUP_TEMP_MODELS and temperature is not None:
            body["temperature"] = temperature
        return body

    @staticmethod
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    @staticmethod
    def _maybe_convert_to_dict(value: Any) -> dict[str, Any] | None:
        if value is None or isinstance(value, dict):
            return value
        if callable(getattr(value, "model_dump", None)):
            return value.model_dump()
        return None

    @classmethod
    def _extract_usage(cls, response: Any) -> dict[str, int]:
        response_dict = cls._maybe_convert_to_dict(response)
        if response_dict:
            usage = cls._get(response_dict, "usage")
        else:
            usage = cls._get(response, "usage")

        usage_dict = cls._maybe_convert_to_dict(usage)
        if usage_dict:
            result = {
                "prompt_tokens": int(cls._get(usage_dict, "prompt_tokens") or 0),
                "completion_tokens": int(cls._get(usage_dict, "completion_tokens") or 0),
                "total_tokens": int(cls._get(usage_dict, "total_tokens") or 0)
            }
        elif usage:
            result = {
                "prompt_tokens": int(cls._get(usage, "prompt_tokens") or 0),
                "completion_tokens": int(cls._get(usage, "completion_tokens") or 0),
                "total_tokens": int(cls._get(usage, "total_tokens") or 0)
            }
        else:
            result = {}

        def _get_nested_int(obj: Any, path: tuple[str, ...]) -> int:
            cur = obj
            for segm in path:
                if cur is None:
                    return None
                if isinstance(cur, dict):
                    cur = cur.get(segm)
                else:
                    cur = getattr(cur, segm, None)
            return int(cur or 0)
        for path in (
            ("prompt_tokens_details", "cached_tokens"),
            ("cached_tokens",),
            ("prompt_cache_hit_tokens",)
        ):
            cached = _get_nested_int(usage_dict, path)
            if not cached and usage:
                cached = _get_nested_int(usage, path)
            if cached:
                result["cached_tokens"] = cached
                break

        return result

    @classmethod
    def _extract_content(cls, content: Any) -> str | None:
        if not content:
            return ""
        if isinstance(content, str):
            return content
        elif isinstance(content, dict):
            return content.get("text")
        elif isinstance(content, list):
            parts = []
            for item in content:
                item_dict = cls._maybe_convert_to_dict(item)
                if item_dict:
                    parts.append(cls._extract_content(item_dict))
                else:
                    parts.append(cls._extract_content(item))
            return "".join(parts)
        else:
            return getattr(content, "text", None)

    @classmethod
    def _extract_tc_extras(cls, tc: Any) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
        extra_content = cls._maybe_convert_to_dict(cls._get(tc, "extra_content"))
        tc_dict = cls._maybe_convert_to_dict(tc)
        prov = None
        fn_prov = None
        if tc_dict is not None:
            leftover = {k: v for k, v in tc_dict.items()
                        if k not in _STANDARD_TC_KEYS and k != "extra_content" and v is not None}
            if leftover:
                prov = leftover
            fn = cls._maybe_convert_to_dict(tc_dict.get("function"))
            if fn is not None:
                fn_leftover = {k: v for k, v in fn.items()
                            if k not in _STANDARD_FN_KEYS and v is not None}
                if fn_leftover:
                    fn_prov = fn_leftover
        else:
            prov = cls._maybe_convert_to_dict(cls._get(tc, "provider_specific_fields"))
            fn_obj = cls._get(tc, "function")
            if fn_obj is not None:
                fn_prov = cls._maybe_convert_to_dict(cls._get(fn_obj, "provider_specific_fields"))

        return extra_content, prov, fn_prov

    def _parse(self, response: Any) -> LLMResponse:
        if isinstance(response, str):
            return LLMResponse(
                content=response,
                finish_reason="stop",
            )

        response_dict = self._maybe_convert_to_dict(response)
        if response_dict:
            choices = response_dict.get("choices", [])
            if not choices:
                content = self._extract_content(
                    response_dict.get("content") or response_dict.get("output_text")
                )
                reasoning_content = self._extract_content(
                    response_dict.get("reasoning_content")
                )
                if content is not None:
                    return LLMResponse(
                        content=content or reasoning_content,
                        reasoning_content=reasoning_content,
                        finish_reason=str(response_dict.get("finish_reason") or "stop"),
                        usage=self._extract_usage(response_dict),
                    )
                return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")
            choice_dict = self._maybe_convert_to_dict(choices[0])
            content = self._extract_content(choice_dict.get("message", {}).get("content"))
            finish_reason = choice_dict.get("finish_reason")
            usage = self._extract_usage(response_dict)
            reasoning = self._extract_content(choice_dict.get("message", {}).get("reasoning"))

            # 只考虑choices[0]
            raw_tool_calls = []
            tool_calls = choice_dict.get("message", {}).get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                raw_tool_calls.extend(tool_calls)

            parsed_tool_calls = []
            for tc in raw_tool_calls:
                tc_map = self._maybe_convert_to_dict(tc) or {}
                fn = self._maybe_convert_to_dict(tc_map.get("function")) or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    args = json_repair.loads(args)
                ec, prov, fn_prov = self._extract_tc_extras(tc)
                parsed_tool_calls.append(ToolCallRequest(
                    id=str(tc_map.get("id") or _short_tool_id()),
                    name=str(fn.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                    extra_content=ec,
                    provider_specific_fields=prov,
                    function_provider_specific_fields=fn_prov,
                ))

            return LLMResponse(
                content=content or reasoning,
                tool_calls=parsed_tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_content=reasoning,
            )

        if not response.choices:
            return LLMResponse(
                content="Error: API returned empty choices.",
                finish_reason="error",
            )

        choice = response.choices[0]
        msg = choice.message
        content = getattr(msg, "content")
        finish_reason = choice.finish_reason
        reasoning_content = getattr(msg, "reasoning_content", None) or None
        if not reasoning_content and getattr(msg, "reasoning", None):
            reasoning_content = getattr(msg, "reasoning", None)

        raw_tool_calls: list[Any] = []
        # 只考虑choices[0]
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            raw_tool_calls.extend(msg.tool_calls)

        tool_calls = []
        for tc in raw_tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)
            ec, prov, fn_prov = self._extract_tc_extras(tc)
            tool_calls.append(ToolCallRequest(
                id=str(getattr(tc, "id", None) or _short_tool_id()),
                name=tc.function.name,
                arguments=args,
                extra_content=ec,
                provider_specific_fields=prov,
                function_provider_specific_fields=fn_prov,
            ))

        reasoning_content = getattr(msg, "reasoning_content", None) or None
        if not reasoning_content and getattr(msg, "reasoning", None):
            reasoning_content = msg.reasoning

        return LLMResponse(
            content=content or reasoning_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=self._extract_usage(response),
            reasoning_content=reasoning_content,
        )

    def _parse_chunks(self, chunks: list[Any]) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_accum: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        finish_reason = "stop"

        for chunk in chunks:
            chunk_dict = self._maybe_convert_to_dict(chunk)
            if chunk_dict is not None:
                choices = chunk_dict.get("choices", [])
                choice = choices[0] if choices else None
                delta = choice.get("delta") if choice else None
                fr = choice.get("finish_reason") if choice else None
                if chunk_dict.get("usage"):
                    usage = self._extract_usage(chunk_dict)
            else:
                choices = getattr(chunk, "choices", []) or []
                choice = choices[0] if choices else None
                delta = choice.delta if choice else None
                fr = getattr(choice, "finish_reason", None) if choice else None
                if getattr(chunk, "usage", None):
                    usage = self._extract_usage(chunk)

            if fr:
                finish_reason = fr
            if delta is None:
                continue

            c = self._get(delta, "content")
            if c:
                content_parts.append(c)

            r = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
            if r:
                reasoning_parts.append(r)

            tc_list = self._get(delta, "tool_calls")
            if tc_list:
                for tc in tc_list:
                    tc_dict = self._maybe_convert_to_dict(tc) or {}
                    idx = tc_dict.get("index", 0) if isinstance(tc, dict) else getattr(tc, "index", 0)

                    if idx not in tool_call_accum:
                        tool_call_accum[idx] = {
                            "id": None, "name": "", "arguments": "",
                            "extra_content": None,
                            "provider_specific_fields": None,
                            "function_provider_specific_fields": None,
                        }
                    acc = tool_call_accum[idx]

                    tc_id = tc_dict.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        acc["id"] = str(tc_id)

                    fn = tc_dict.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                    if fn:
                        fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                        if fn_name:
                            acc["name"] = fn_name
                        fn_args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                        if fn_args:
                            acc["arguments"] += fn_args

                    ec, prov, fn_prov = self._extract_tc_extras(tc)
                    if ec:
                        acc["extra_content"] = ec
                    if prov:
                        acc["provider_specific_fields"] = prov
                    if fn_prov:
                        acc["function_provider_specific_fields"] = fn_prov

        tool_calls: list[ToolCallRequest] = []
        for idx in sorted(tool_call_accum.keys()):
            acc = tool_call_accum[idx]
            args_str = acc["arguments"]
            try:
                args = json_repair.loads(args_str) if args_str.strip() else {}
            except Exception:
                args = {}
            tool_calls.append(ToolCallRequest(
                id=str(acc["id"] or _short_tool_id()),
                name=acc["name"],
                arguments=args if isinstance(args, dict) else {},
                extra_content=acc["extra_content"],
                provider_specific_fields=acc["provider_specific_fields"],
                function_provider_specific_fields=acc["function_provider_specific_fields"],
            ))

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts) if reasoning_parts else None
        # Some models (e.g. DeepSeek) emit the visible response through
        # reasoning_content rather than content.  Merge so downstream consumers
        # never receive an empty content when reasoning is present.
        if not content and reasoning:
            content = reasoning
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None
    ) -> LLMResponse:
        if not self._client:
            self._build_client()
        assert self._client is not None
        start = time.time()
        try:
            body = self._build_chat_completion_body(
                messages, tools, model,
                max_tokens or self.max_tokens,
                temperature or self.temperature
            )
            response = await self._client.chat.completions.create(
                **body
            )

            llm_response = self._parse(response)
            llm_response.latency_s = time.time() - start
            return llm_response
        except Exception as e:
            logger.opt(exception=True).warning("chat() failed: {}", e)
            return LLMResponse(
                content=f"OpenAI API error: {e}",
                finish_reason="error",
                latency_s=time.time() - start
            )


    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    ) -> LLMResponse:
        if not self._client:
            self._build_client()
        assert self._client is not None
        start = time.time()
        try:
            body = self._build_chat_completion_body(
                messages, tools, model,
                max_tokens or self.max_tokens,
                temperature or self.temperature
            )
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

            stream = await self._client.chat.completions.create(**body)

            chunks: list[Any] = []
            async for chunk in stream:
                chunks.append(chunk)

                chunk_dict = self._maybe_convert_to_dict(chunk)
                if chunk_dict is not None:
                    choices = chunk_dict.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else None
                else:
                    delta = chunk.choices[0].delta if chunk.choices else None

                if delta is None:
                    continue

                c = self._get(delta, "content")
                if c and on_content_delta:
                    await on_content_delta(c)

                r = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
                if r and on_thinking_delta:
                    await on_thinking_delta(r)

                tc_list = self._get(delta, "tool_calls")
                if tc_list and on_tool_call_delta:
                    for tc in tc_list:
                        tc_dict = self._maybe_convert_to_dict(tc)
                        if tc_dict is not None:
                            await on_tool_call_delta(tc_dict)
                        else:
                            await on_tool_call_delta({
                                "index": getattr(tc, "index", 0),
                                "id": getattr(tc, "id", None),
                                "function": {
                                    "name": tc.function.name if hasattr(tc, "function") and tc.function else None,
                                    "arguments": tc.function.arguments if hasattr(tc, "function") and tc.function else None,
                                }
                            })

            llm_response = self._parse_chunks(chunks)
            llm_response.latency_s = time.time() - start
            return llm_response
        except Exception as e:
            return LLMResponse(
                content=f"OpenAI API streaming error: {e}",
                # error=self._handle_error(e),
                latency_s=time.time() - start,
                finish_reason="error",
            )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    llm = OpenAICompatibleProvider(
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENAI_API_BASE"),
        name=os.getenv("PROVIDER_NAME", "openrouter"),
        default_model=os.getenv("LLM_MODEL_ID", "deepseek/deepseek-v4-flash")
    )
    message = [{"role": "user", "content": "宇宙中有外星人吗？"}]
    def test_chat(message):
        result = asyncio.run(llm.chat(message))
        print(result)

    def test_chat_stream(message):
        async def on_content_delta(delta: str):
            print(delta, end="", flush=True)
        async def on_thinking_delta(delta: str):
            print(delta, end="", flush=True)
        async def on_tool_call_delta(delta: dict[str, Any]):
            print(delta, end="", flush=True)
        asyncio.run(llm.chat_stream(
            message,
            on_content_delta=on_content_delta,
            on_thinking_delta=on_thinking_delta,
            on_tool_call_delta=on_tool_call_delta,
        ))

    test_chat_stream(message)

