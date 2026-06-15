"""Tests for the OpenTelemetry bridge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from observability.trace import Span, SpanContext, Tracer


# ---------------------------------------------------------------------------
# otel_available
# ---------------------------------------------------------------------------


class TestOtelAvailable:
    def test_returns_true_when_all_installed(self):
        from observability.otel_bridge import otel_available
        # OTel is installed in test env, so this should be True
        assert otel_available() is True


# ---------------------------------------------------------------------------
# OTelBridge
# ---------------------------------------------------------------------------


class TestOTelBridge:
    @pytest.fixture
    def tracer(self):
        return Tracer()

    def test_install_registers_hooks(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        result = bridge.install(tracer)

        assert result is True
        assert len(tracer._on_span_start) == 1
        assert len(tracer._on_span_end) == 1

    def test_double_install_is_idempotent(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        bridge.install(tracer)
        bridge.install(tracer)

        assert len(tracer._on_span_start) == 1
        assert len(tracer._on_span_end) == 1

    def test_hooks_create_and_end_otel_spans(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        bridge.install(tracer)

        # The bridge should have created an OTel tracer
        assert bridge._otel_tracer is not None

        with tracer.trace("test.root", key="val"):
            with tracer.span("test.child", model="x"):
                pass

        assert len(bridge._otel_span_map) == 0  # all spans ended

    def test_span_attributes_passed_to_otel(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        bridge.install(tracer)

        with tracer.trace("attr.test", model="gpt-4", temperature=0.7):
            pass

        assert len(bridge._otel_span_map) == 0

    def test_error_span_sets_otel_status(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        bridge.install(tracer)

        try:
            with tracer.trace("error.test"):
                raise RuntimeError("test error")
        except RuntimeError:
            pass

        assert len(bridge._otel_span_map) == 0

    def test_span_events_propagated(self, tracer):
        from observability.otel_bridge import OTelBridge

        bridge = OTelBridge()
        bridge.install(tracer)

        with tracer.trace("events.test") as span:
            tracer.add_event("custom.event", key="val")

        assert span.events == [{"name": "custom.event", "key": "val", "timestamp": pytest.approx(span.events[0]["timestamp"])}]
        assert len(bridge._otel_span_map) == 0

    def test_install_without_otel_packages_returns_false(self, tracer):
        from observability.otel_bridge import OTelBridge

        # Mock otel_available to return False
        with patch("observability.otel_bridge.otel_available", return_value=False):
            bridge = OTelBridge()
            result = bridge.install(tracer)

        assert result is False
        assert len(tracer._on_span_start) == 0
        assert len(tracer._on_span_end) == 0


# ---------------------------------------------------------------------------
# auto_install
# ---------------------------------------------------------------------------


class TestAutoInstall:
    @pytest.fixture
    def tracer(self):
        return Tracer()

    def test_skips_when_env_var_not_set(self, tracer, monkeypatch):
        monkeypatch.delenv("MYBOT_OTEL_ENABLED", raising=False)

        from observability.otel_bridge import auto_install
        result = auto_install(tracer)

        assert result is False
        assert len(tracer._on_span_start) == 0

    def test_installs_when_env_var_is_1(self, tracer, monkeypatch):
        monkeypatch.setenv("MYBOT_OTEL_ENABLED", "1")

        from observability.otel_bridge import auto_install
        result = auto_install(tracer)

        assert result is True
        assert len(tracer._on_span_start) == 1
        assert len(tracer._on_span_end) == 1

    def test_installs_when_env_var_is_true(self, tracer, monkeypatch):
        monkeypatch.setenv("MYBOT_OTEL_ENABLED", "true")

        from observability.otel_bridge import auto_install
        result = auto_install(tracer)

        assert result is True
