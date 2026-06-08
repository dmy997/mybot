"""Tests for observability/log.py."""

from __future__ import annotations

from observability.log import (
    AgentRunEvent,
    LLMCallEvent,
    LogConfig,
    SessionEvent,
    ToolCallEvent,
    _to_dict,
    emit,
    init_logging,
)


class TestLogConfig:
    def test_defaults(self):
        c = LogConfig()
        assert c.level == "WARNING"
        assert c.json_format is False
        assert c.log_dir is None

    def test_custom(self, tmp_path):
        c = LogConfig(level="INFO", log_dir=tmp_path)
        assert c.level == "INFO"
        assert c.log_dir == tmp_path

    def test_init_logging_idempotent(self):
        """Second call is a no-op."""
        config = LogConfig()
        init_logging(config)
        assert config._initialized is True
        # Should not raise
        init_logging(config)

    def test_init_logging_with_log_dir(self, tmp_path):
        log_dir = tmp_path / "logs"
        config = LogConfig(log_dir=log_dir)
        init_logging(config)
        # Should have created the directory
        assert log_dir.exists()


class TestEventDataclasses:
    def test_llm_call_event(self):
        e = LLMCallEvent(
            model="gpt-4",
            latency_ms=1234.5,
            messages_count=5,
            tools_count=3,
            tokens_in=100,
            tokens_out=50,
            tokens_total=150,
            finish_reason="stop",
        )
        d = _to_dict(e)
        assert d["model"] == "gpt-4"
        assert d["tokens_total"] == 150
        assert d["error"] is None

    def test_tool_call_event(self):
        e = ToolCallEvent(tool_name="bash", success=True, latency_ms=100.0)
        d = _to_dict(e)
        assert d["tool_name"] == "bash"
        assert d["success"] is True

    def test_tool_call_event_error(self):
        e = ToolCallEvent(tool_name="bash", success=False, latency_ms=500.0, error="timeout")
        d = _to_dict(e)
        assert d["error"] == "timeout"

    def test_session_event(self):
        e = SessionEvent(session_key="abc123", action="created", message_count=0)
        d = _to_dict(e)
        assert d["session_key"] == "abc123"
        assert d["action"] == "created"

    def test_agent_run_event(self):
        e = AgentRunEvent(
            session_key="abc",
            paradigm="react",
            steps=5,
            total_latency_ms=3000.0,
            stop_reason="stop",
        )
        d = _to_dict(e)
        assert d["paradigm"] == "react"
        assert d["steps"] == 5


class TestEmit:
    def test_emit_does_not_raise(self):
        """emit() should not raise under normal conditions."""
        emit(LLMCallEvent(
            model="test", latency_ms=1.0, messages_count=1, tools_count=0,
            tokens_in=10, tokens_out=5, tokens_total=15, finish_reason="stop",
        ))
        emit(ToolCallEvent(tool_name="read", success=True, latency_ms=1.0))

    def test_emit_custom_level(self):
        """emit with a non-default level."""
        emit(ToolCallEvent(tool_name="read", success=True, latency_ms=1.0), level="DEBUG")


class TestToDict:
    def test_dataclass(self):
        d = _to_dict(LLMCallEvent(
            model="x", latency_ms=1.0, messages_count=1, tools_count=0,
            tokens_in=10, tokens_out=5, tokens_total=15, finish_reason="stop",
        ))
        assert isinstance(d, dict)
        assert "model" in d

    def test_primitive_passthrough(self):
        assert _to_dict(42) == 42
        assert _to_dict("hello") == "hello"
        assert _to_dict(None) is None

    def test_list_passthrough(self):
        result = _to_dict([1, 2, 3])
        assert result == [1, 2, 3]
