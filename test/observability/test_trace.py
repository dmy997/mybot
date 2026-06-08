"""Tests for observability/trace.py."""

from __future__ import annotations

import pytest

from observability.trace import Span, SpanContext, Tracer, tracer


class TestSpanContext:
    def test_create(self):
        ctx = SpanContext(trace_id="abc", span_id="123", parent_span_id=None)
        assert ctx.trace_id == "abc"
        assert ctx.span_id == "123"
        assert ctx.parent_span_id is None

    def test_with_parent(self):
        ctx = SpanContext(trace_id="abc", span_id="child", parent_span_id="root")
        assert ctx.parent_span_id == "root"


class TestSpan:
    def test_latency_unfinished(self):
        s = Span(name="test", context=SpanContext("a", "b", None))
        assert s.latency_ms >= 0

    def test_latency_finished(self):
        s = Span(name="test", context=SpanContext("a", "b", None))
        s.end_time = s.start_time + 1.0
        assert s.latency_ms == pytest.approx(1000.0, rel=0.1)


class TestTracerStartTrace:
    def test_start_trace_creates_new_trace_id(self):
        t = Tracer()
        span1 = t.start_trace("op1")
        span2 = t.start_trace("op2")
        assert span1.context.trace_id != span2.context.trace_id
        assert span1.context.parent_span_id is None

    def test_start_trace_isolation(self):
        """start_trace ignores any current span and creates a new root."""
        t = Tracer()
        t.start_trace("first")
        span2 = t.start_trace("second")
        assert span2.context.parent_span_id is None


class TestTracerStartSpan:
    def test_start_span_creates_child(self):
        t = Tracer()
        root = t.start_trace("root")
        child = t.start_span("child")
        assert child.context.trace_id == root.context.trace_id
        assert child.context.parent_span_id == root.context.span_id

    def test_start_span_without_parent_creates_trace(self):
        t = Tracer()
        span = t.start_span("orphan")
        assert span.context.parent_span_id is None

    def test_start_span_nesting(self):
        t = Tracer()
        root = t.start_trace("root")
        child1 = t.start_span("child1")
        child2 = t.start_span("child2")
        assert child2.context.parent_span_id == child1.context.span_id
        assert child2.context.trace_id == root.context.trace_id


class TestTracerEndSpan:
    def test_end_span_sets_timestamp(self):
        t = Tracer()
        span = t.start_trace("op")
        assert span.end_time is None
        t.end_span(span)
        assert span.end_time is not None

    def test_end_span_restores_parent(self):
        t = Tracer()
        root = t.start_trace("root")
        child = t.start_span("child")
        t.end_span(child)
        # After ending child, the parent span is restored as current
        current = t.current_span()
        assert current is root
        # The current span should be root (same trace_id as child but no parent)
        # Actually current_span returns None after end_span since we can't
        # restore the parent object. This is documented behavior.
        # We just verify child is properly finalized.
        assert child.end_time is not None
        assert child.status == "ok"

    def test_end_span_with_error(self):
        t = Tracer()
        span = t.start_trace("op")
        t.end_span(span, "error")
        assert span.status == "error"


class TestTracerContextManager:
    def test_trace_context_manager_success(self):
        t = Tracer()
        with t.trace("root", key="value") as span:
            assert span.name == "root"
            assert span.attributes["key"] == "value"
        assert span.status == "ok"
        assert span.end_time is not None

    def test_trace_context_manager_error(self):
        t = Tracer()
        with pytest.raises(ValueError):
            with t.trace("root") as span:
                raise ValueError("boom")
        assert span.status == "error"

    def test_span_context_manager_nesting(self):
        t = Tracer()
        with t.trace("root") as root:
            with t.span("child") as child:
                assert child.context.trace_id == root.context.trace_id
                assert child.context.parent_span_id == root.context.span_id
            assert child.status == "ok"
        assert root.status == "ok"

    def test_nested_error_propagates(self):
        t = Tracer()
        with pytest.raises(ValueError):
            with t.trace("root") as root:
                with t.span("child1"):
                    pass
                with t.span("child2"):
                    raise ValueError("mid-span error")
        assert root.status == "error"


class TestTracerHelpers:
    def test_current_span_none_initially(self):
        t = Tracer()
        assert t.current_span() is None

    def test_current_trace_id(self):
        t = Tracer()
        assert t.current_trace_id() is None
        span = t.start_trace("op")
        assert t.current_trace_id() == span.context.trace_id

    def test_set_attribute(self):
        t = Tracer()
        t.start_trace("op")
        t.set_attribute("extra", 42)
        assert t.current_span().attributes["extra"] == 42

    def test_add_event(self):
        t = Tracer()
        t.start_trace("op")
        t.add_event("checkpoint", data="x")
        assert len(t.current_span().events) == 1
        assert t.current_span().events[0]["name"] == "checkpoint"
        assert t.current_span().events[0]["data"] == "x"


class TestGlobalTracer:
    """Verify the module-level 'tracer' singleton works correctly."""

    def test_global_tracer_is_tracer_instance(self):
        from observability.trace import Tracer
        assert isinstance(tracer, Tracer)

    def test_global_tracer_basic_span(self):
        with tracer.trace("global.test"):
            pass  # should not raise
