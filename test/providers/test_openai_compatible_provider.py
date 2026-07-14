"""Tests for OpenAICompatibleProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.base import LLMResponse
from providers.openai_compatible_provider import (
    _ALNUM,
    _NO_SUP_TEMP_MODELS,
    OpenAICompatibleProvider,
    _short_tool_id,
)

# ---------------------------------------------------------------------------
# Lightweight fakes for object-path testing (no model_dump — avoids
# _maybe_convert_to_dict short-circuiting into the dict branch).
# ---------------------------------------------------------------------------

class _FakeBase:
    """Base for non-dict fakes — model_dump=None avoids _maybe_convert_to_dict."""
    model_dump = None


class _FakeUsage(_FakeBase):
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeFunction(_FakeBase):
    def __init__(self, name="", arguments="", **extras):
        self.name = name
        self.arguments = arguments
        for k, v in extras.items():
            setattr(self, k, v)


class _FakeToolCall(_FakeBase):
    def __init__(self, id="", function=None, index=0, **extras):
        self.id = id
        self.function = function or _FakeFunction()
        self.index = index
        for k, v in extras.items():
            setattr(self, k, v)


class _FakeMessage(_FakeBase):
    def __init__(self, content=None, tool_calls=None, reasoning_content=None, reasoning=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content
        if reasoning is not None:
            self.reasoning = reasoning


class _FakeChoice(_FakeBase):
    def __init__(self, message=None, delta=None, finish_reason=None):
        self.message = message
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeResponse(_FakeBase):
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _FakeChunk(_FakeBase):
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    return OpenAICompatibleProvider(
        api_key="test-key",
        api_base="https://api.openai.com/v1",
        default_model="gpt-4o",
    )


# ---------------------------------------------------------------------------
# _short_tool_id
# ---------------------------------------------------------------------------

class TestShortToolId:
    def test_length(self):
        tid = _short_tool_id()
        assert len(tid) == 9

    def test_alphanumeric(self):
        for _ in range(100):
            assert all(c in _ALNUM for c in _short_tool_id())

    def test_unique(self):
        ids = {_short_tool_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# _get
# ---------------------------------------------------------------------------

class TestGet:
    def test_dict_existing_key(self, provider):
        assert provider._get({"a": 1, "b": 2}, "a") == 1

    def test_dict_missing_key(self, provider):
        assert provider._get({"a": 1}, "b") is None

    def test_object_attr(self, provider):
        obj = _FakeUsage(foo="bar")
        assert provider._get(obj, "foo") == "bar"

    def test_object_missing_attr(self, provider):
        obj = object()
        assert provider._get(obj, "nope") is None


# ---------------------------------------------------------------------------
# _maybe_convert_to_dict
# ---------------------------------------------------------------------------

class TestMaybeConvertToDict:
    def test_none(self, provider):
        assert provider._maybe_convert_to_dict(None) is None

    def test_dict_passthrough(self, provider):
        d = {"key": "val"}
        assert provider._maybe_convert_to_dict(d) is d

    def test_model_dump(self, provider):
        obj = MagicMock()
        obj.model_dump.return_value = {"x": 1}
        assert provider._maybe_convert_to_dict(obj) == {"x": 1}

    def test_no_conversion(self, provider):
        # plain object without model_dump
        assert provider._maybe_convert_to_dict(object()) is None


# ---------------------------------------------------------------------------
# _extract_usage
# ---------------------------------------------------------------------------

class TestExtractUsage:
    def test_from_dict(self, provider):
        resp = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        result = provider._extract_usage(resp)
        assert result == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    def test_from_object(self, provider):
        usage = _FakeUsage(prompt_tokens=5, completion_tokens=15, total_tokens=20)
        resp = _FakeResponse(usage=usage)
        result = provider._extract_usage(resp)
        assert result == {"prompt_tokens": 5, "completion_tokens": 15, "total_tokens": 20}

    def test_nested_dict_usage(self, provider):
        usage_obj = _FakeUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        resp = MagicMock()
        resp.model_dump.return_value = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        resp.usage = usage_obj
        result = provider._extract_usage(resp)
        assert result["prompt_tokens"] == 10  # dict path wins

    def test_cached_tokens_via_details(self, provider):
        resp = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 5},
            }
        }
        result = provider._extract_usage(resp)
        assert result["cached_tokens"] == 5

    def test_cached_tokens_direct(self, provider):
        resp = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "cached_tokens": 7,
            }
        }
        result = provider._extract_usage(resp)
        assert result["cached_tokens"] == 7

    def test_cached_tokens_prompt_cache_hit(self, provider):
        resp = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_cache_hit_tokens": 3,
            }
        }
        result = provider._extract_usage(resp)
        assert result["cached_tokens"] == 3

    def test_empty_usage(self, provider):
        assert provider._extract_usage(object()) == {}


# ---------------------------------------------------------------------------
# _extract_content
# ---------------------------------------------------------------------------

class TestExtractContent:
    def test_string(self, provider):
        assert provider._extract_content("hello") == "hello"

    def test_dict_with_text(self, provider):
        assert provider._extract_content({"text": "world"}) == "world"

    def test_list_of_strings(self, provider):
        assert provider._extract_content(["a", "b", "c"]) == "abc"

    def test_list_of_dicts(self, provider):
        assert provider._extract_content([{"text": "x"}, {"text": "y"}]) == "xy"

    def test_object_with_text(self, provider):
        obj = _FakeUsage(text="hello")
        assert provider._extract_content(obj) == "hello"

    def test_falsy_value(self, provider):
        assert provider._extract_content("") == ""
        assert provider._extract_content(None) == ""


# ---------------------------------------------------------------------------
# _extract_tc_extras
# ---------------------------------------------------------------------------

class TestExtractTcExtras:
    def test_standard_keys_only(self, provider):
        tc = {"id": "call_1", "type": "function", "index": 0, "function": {"name": "foo", "arguments": "{}"}}
        ec, prov, fn_prov = provider._extract_tc_extras(tc)
        assert ec is None
        assert prov is None
        assert fn_prov is None

    def test_extra_content(self, provider):
        tc = {"id": "call_1", "type": "function", "index": 0,
              "function": {"name": "foo", "arguments": "{}"},
              "extra_content": {"custom": "val"}}
        ec, prov, fn_prov = provider._extract_tc_extras(tc)
        assert ec == {"custom": "val"}

    def test_provider_specific_tc_fields(self, provider):
        tc = {"id": "call_1", "type": "function", "index": 0,
              "function": {"name": "foo", "arguments": "{}"},
              "custom_field": True, "another": 42}
        ec, prov, fn_prov = provider._extract_tc_extras(tc)
        assert prov == {"custom_field": True, "another": 42}

    def test_provider_specific_fn_fields(self, provider):
        tc = {"id": "call_1", "type": "function", "index": 0,
              "function": {"name": "foo", "arguments": "{}", "fn_extra": 99}}
        ec, prov, fn_prov = provider._extract_tc_extras(tc)
        assert fn_prov == {"fn_extra": 99}

    def test_object_with_provider_specific_fields(self, provider):
        fn = _FakeFunction(name="foo", arguments="{}", provider_specific_fields={"extra": "data"})
        tc = _FakeToolCall(
            id="call_1", function=fn, index=0,
            provider_specific_fields={"tc_extra": 1},
        )
        ec, prov, fn_prov = provider._extract_tc_extras(tc)
        assert prov == {"tc_extra": 1}
        assert fn_prov == {"extra": "data"}


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_defaults(self):
        p = OpenAICompatibleProvider()
        assert p._default_model == "deepseek/deepseek-v4-flash"
        assert p.is_local is False
        assert p._client is None

    def test_openrouter_by_name(self):
        p = OpenAICompatibleProvider(api_key="k", name="openrouter")
        assert "HTTP-Referer" in p._default_headers
        assert p._default_headers["HTTP-Referer"] == "https://github.com/dmy997/mybot"

    def test_openrouter_by_api_base(self):
        p = OpenAICompatibleProvider(api_key="k", api_base="https://openrouter.ai/api/v1")
        assert "HTTP-Referer" in p._default_headers

    def test_no_openrouter_headers_for_other_providers(self):
        p = OpenAICompatibleProvider(api_key="k", api_base="https://api.openai.com/v1")
        assert not hasattr(p, "_default_headers") or p._default_headers == {}


# ---------------------------------------------------------------------------
# _build_chat_completion_body
# ---------------------------------------------------------------------------

class TestBuildChatCompletionBody:
    def test_basic(self, provider):
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            None,
        )
        assert body["model"] == "gpt-4o"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert "tools" not in body

    def test_with_tools(self, provider):
        tools = [{"type": "function", "function": {"name": "test"}}]
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            tools,
        )
        assert body["tools"] == tools

    def test_empty_tools_not_included(self, provider):
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            [],
        )
        assert "tools" not in body

    def test_temperature_included(self, provider):
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            None,
            temperature=0.5,
        )
        assert body["temperature"] == 0.5

    def test_temperature_skipped_for_no_temp_models(self, provider):
        for model_name in _NO_SUP_TEMP_MODELS:
            body = provider._build_chat_completion_body(
                [{"role": "user", "content": "hi"}],
                None,
                model=model_name,
                temperature=0.5,
            )
            assert "temperature" not in body

    def test_explicit_model(self, provider):
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            None,
            model="gpt-4o-mini",
        )
        assert body["model"] == "gpt-4o-mini"

    def test_max_tokens(self, provider):
        body = provider._build_chat_completion_body(
            [{"role": "user", "content": "hi"}],
            None,
            max_tokens=100,
        )
        assert body["max_tokens"] == 100


# ---------------------------------------------------------------------------
# _parse — dict path
# ---------------------------------------------------------------------------

class TestParseDict:
    def test_simple_string(self, provider):
        resp = provider._parse("plain response")
        assert resp.content == "plain response"
        assert resp.finish_reason == "stop"

    def test_dict_with_choices(self, provider):
        resp = provider._parse({
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
        assert resp.content == "hello"
        assert resp.finish_reason == "stop"
        assert resp.usage["total_tokens"] == 2

    def test_dict_without_choices_has_content(self, provider):
        resp = provider._parse({
            "content": "direct content",
            "finish_reason": "stop",
        })
        assert resp.content == "direct content"

    def test_dict_without_choices_has_output_text(self, provider):
        resp = provider._parse({
            "output_text": "generated text",
            "finish_reason": "stop",
        })
        assert resp.content == "generated text"

    def test_dict_empty_choices_no_content(self, provider):
        # When choices is empty and neither 'content' nor 'output_text' is a
        # non-None value, _extract_content returns "" which triggers the
        # content path ("" is not None).
        resp = provider._parse({"choices": [], "content": None})
        assert resp.content == ""

    def test_dict_with_reasoning(self, provider):
        resp = provider._parse({
            "choices": [{"message": {"content": "answer", "reasoning": "think step by step"}, "finish_reason": "stop"}],
        })
        assert resp.reasoning_content == "think step by step"

    def test_dict_with_tool_calls(self, provider):
        resp = provider._parse({
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'}}
            ]}, "finish_reason": "tool_calls"}],
        })
        assert resp.finish_reason == "tool_calls"
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "NYC"}

    def test_dict_tool_call_args_not_json(self, provider):
        resp = provider._parse({
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": {"already": "dict"}}}
            ]}, "finish_reason": "tool_calls"}],
        })
        assert resp.tool_calls[0].arguments == {"already": "dict"}

    def test_dict_tool_call_with_extras(self, provider):
        resp = provider._parse({
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "type": "function", "index": 0,
                 "function": {"name": "f", "arguments": "{}", "fn_extra": 1},
                 "tc_extra": 2}
            ]}, "finish_reason": "tool_calls"}],
        })
        tc = resp.tool_calls[0]
        assert tc.provider_specific_fields == {"tc_extra": 2}
        assert tc.function_provider_specific_fields == {"fn_extra": 1}


# ---------------------------------------------------------------------------
# _parse — object path (using fakes without model_dump)
# ---------------------------------------------------------------------------

class TestParseObject:
    def test_object_with_choices(self, provider):
        msg = _FakeMessage(content="obj hello")
        choice = _FakeChoice(message=msg, finish_reason="stop")
        resp = _FakeResponse(choices=[choice])

        result = provider._parse(resp)
        assert result.content == "obj hello"
        assert result.finish_reason == "stop"

    def test_object_no_choices(self, provider):
        resp = _FakeResponse()
        result = provider._parse(resp)
        assert result.finish_reason == "error"

    def test_object_with_tool_calls(self, provider):
        fn = _FakeFunction(name="search", arguments='{"q": "test"}')
        tc = _FakeToolCall(id="call_2", function=fn)
        msg = _FakeMessage(tool_calls=[tc])
        choice = _FakeChoice(message=msg, finish_reason="tool_calls")
        resp = _FakeResponse(choices=[choice])

        result = provider._parse(resp)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"q": "test"}

    def test_object_with_reasoning(self, provider):
        msg = _FakeMessage(content="answer", reasoning_content="thinking...")
        choice = _FakeChoice(message=msg, finish_reason="stop")
        resp = _FakeResponse(choices=[choice])

        result = provider._parse(resp)
        assert result.reasoning_content == "thinking..."

    def test_object_reasoning_fallback(self, provider):
        msg = _FakeMessage(content="answer", reasoning="fallback thinking")
        choice = _FakeChoice(message=msg, finish_reason="stop")
        resp = _FakeResponse(choices=[choice])

        result = provider._parse(resp)
        assert result.reasoning_content == "fallback thinking"


# ---------------------------------------------------------------------------
# _parse_chunks
# ---------------------------------------------------------------------------

class TestParseChunks:
    def test_content_accumulation(self, provider):
        chunks = [
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " World"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.content == "Hello World"
        assert resp.finish_reason == "stop"
        assert resp.usage["total_tokens"] == 3

    def test_reasoning_accumulation(self, provider):
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "step 1"}}]},
            {"choices": [{"delta": {"reasoning_content": " step 2"}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.reasoning_content == "step 1 step 2"

    def test_reasoning_fallback_key(self, provider):
        chunks = [
            {"choices": [{"delta": {"reasoning": "think"}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.reasoning_content == "think"

    def test_tool_call_accumulation(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":'}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ' "NYC"}'}}
            ]}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "NYC"}

    def test_multiple_tool_calls(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"index": 1, "id": "c2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ]}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert len(resp.tool_calls) == 2
        assert resp.tool_calls[0].name == "f1"
        assert resp.tool_calls[1].name == "f2"

    def test_tool_call_with_extras(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}", "fn_extra": 1},
                 "tc_extra": 2}
            ]}}]},
        ]
        resp = provider._parse_chunks(chunks)
        tc = resp.tool_calls[0]
        assert tc.provider_specific_fields == {"tc_extra": 2}
        assert tc.function_provider_specific_fields == {"fn_extra": 1}

    def test_empty_chunks(self, provider):
        resp = provider._parse_chunks([])
        assert resp.content == ""
        assert resp.finish_reason == "stop"
        assert resp.tool_calls == []

    def test_usage_in_last_chunk(self, provider):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.usage["total_tokens"] == 6
        assert resp.usage["prompt_tokens"] == 5

    def test_object_chunks(self, provider):
        delta = _FakeMessage(content="obj stream")
        choice = _FakeChoice(delta=delta, finish_reason="stop")
        chunk = _FakeChunk(choices=[choice])

        resp = provider._parse_chunks([chunk])
        assert resp.content == "obj stream"

    def test_none_delta_skipped(self, provider):
        chunks = [
            {"choices": []},
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.content == "ok"

    def test_invalid_json_args_repaired(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "f", "arguments": '{"key": "val"'}}
            ]}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.tool_calls[0].arguments == {"key": "val"}

    def test_non_dict_args_handled(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "f", "arguments": "not json at all"}}
            ]}}]},
        ]
        resp = provider._parse_chunks(chunks)
        assert resp.tool_calls[0].arguments == {}


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

class TestChat:
    @pytest.mark.asyncio
    async def test_successful_call(self, provider):
        mock_client = MagicMock()
        provider._parse = MagicMock(return_value=LLMResponse(content="parsed"))
        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._client = mock_client

        create_mock = AsyncMock()
        mock_client.chat.completions.create = create_mock

        result = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        assert provider._parse.called
        assert result.content == "parsed"
        assert result.latency_s >= 0

    @pytest.mark.asyncio
    async def test_error_handling(self, provider):
        """chat() propagates exceptions so the retry layer can classify and retry."""
        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._client = MagicMock()
        provider._client.chat.completions.create = MagicMock(side_effect=RuntimeError("API down"))

        with pytest.raises(RuntimeError, match="API down"):
            await provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            )


# ---------------------------------------------------------------------------
# chat_stream
# ---------------------------------------------------------------------------

class TestChatStream:
    @pytest.mark.asyncio
    async def test_successful_stream(self, provider):
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeMessage(content="chunk1"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeMessage(content="chunk2"))]),
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._parse_chunks = MagicMock(return_value=LLMResponse(content="chunk1chunk2"))
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_stream())

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        provider._parse_chunks.assert_called_once()
        assert result.content == "chunk1chunk2"
        assert result.latency_s >= 0

    @pytest.mark.asyncio
    async def test_delta_callbacks(self, provider):
        chunks = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " World", "reasoning_content": "hmm"}}]},
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._parse_chunks = MagicMock(return_value=LLMResponse(content="Hello World"))
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_stream())

        content_deltas = []
        thinking_deltas = []

        async def _content(s):
            content_deltas.append(s)

        async def _thinking(s):
            thinking_deltas.append(s)

        await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            on_content_delta=_content,
            on_thinking_delta=_thinking,
        )
        assert content_deltas == ["Hello", " World"]
        assert thinking_deltas == ["hmm"]

    @pytest.mark.asyncio
    async def test_tool_call_delta_callback(self, provider):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":'}}
            ]}}]},
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._parse_chunks = MagicMock(return_value=LLMResponse(content=""))
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_stream())

        tc_deltas = []

        async def _tc(d):
            tc_deltas.append(d)

        await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            on_tool_call_delta=_tc,
        )
        assert len(tc_deltas) == 1
        assert tc_deltas[0]["index"] == 0

    @pytest.mark.asyncio
    async def test_error_handling(self, provider):
        """chat_stream() propagates exceptions so the retry layer can classify and retry."""
        provider._build_chat_completion_body = MagicMock(return_value={"model": "gpt-4o", "messages": []})
        provider._build_client = MagicMock()
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(side_effect=RuntimeError("stream failed"))

        with pytest.raises(RuntimeError, match="stream failed"):
            await provider.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            )

    @pytest.mark.asyncio
    async def test_stream_options_include_usage(self, provider):
        body = {}

        async def mock_stream():
            yield {"choices": [{"delta": {"content": "x"}}]}

        provider._build_chat_completion_body = MagicMock(return_value=body)
        provider._build_client = MagicMock()
        provider._parse_chunks = MagicMock(return_value=LLMResponse(content="x"))
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_stream())

        await provider.chat_stream(messages=[], tools=[])
        assert body.get("stream") is True
        assert body.get("stream_options") == {"include_usage": True}

    @pytest.mark.asyncio
    async def test_none_delta_skipped_in_callback(self, provider):
        chunks = [
            {"choices": []},  # no choices -> delta is None
            {"choices": [{"delta": {"content": "ok"}}]},
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        provider._build_chat_completion_body = MagicMock(return_value={})
        provider._build_client = MagicMock()
        provider._parse_chunks = MagicMock(return_value=LLMResponse(content="ok"))
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_stream())

        content_deltas = []

        async def _content(s):
            content_deltas.append(s)

        await provider.chat_stream(
            messages=[], tools=[],
            on_content_delta=_content,
        )
        assert content_deltas == ["ok"]
