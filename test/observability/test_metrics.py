"""Tests for observability/metrics.py."""

from __future__ import annotations

import pytest

from observability.metrics import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    MetricsRegistrySnapshot,
)


class TestCounter:
    def test_initial_zero(self):
        c = Counter("test")
        assert c.get() == 0

    def test_inc(self):
        c = Counter("test")
        c.inc()
        assert c.get() == 1
        c.inc(5)
        assert c.get() == 6

    def test_name_and_description(self):
        c = Counter("my_counter", description="desc", unit="ops")
        assert c.name == "my_counter"
        assert c.description == "desc"
        assert c.unit == "ops"


class TestGauge:
    def test_initial_zero(self):
        g = Gauge("test")
        assert g.get() == 0.0

    def test_set(self):
        g = Gauge("test")
        g.set(42.0)
        assert g.get() == 42.0

    def test_inc_dec(self):
        g = Gauge("test")
        g.inc()
        assert g.get() == 1.0
        g.inc(2.5)
        assert g.get() == 3.5
        g.dec()
        assert g.get() == 2.5
        g.dec(0.5)
        assert g.get() == 2.0

    def test_negative_values(self):
        g = Gauge("test")
        g.dec(10)
        assert g.get() == -10.0


class TestHistogram:
    def test_initial_empty(self):
        h = Histogram("test")
        s = h.stats()
        assert s["count"] == 0
        assert s["sum"] == 0.0

    def test_single_observation(self):
        h = Histogram("test")
        h.observe(100.0)
        s = h.stats()
        assert s["count"] == 1
        assert s["min"] == 100.0
        assert s["max"] == 100.0
        assert s["avg"] == 100.0
        assert s["p50"] == 100.0

    def test_multiple_observations(self):
        h = Histogram("test")
        for v in [10, 20, 30, 40, 50]:
            h.observe(v)
        s = h.stats()
        assert s["count"] == 5
        assert s["sum"] == 150.0
        assert s["min"] == 10.0
        assert s["max"] == 50.0
        assert s["avg"] == 30.0
        assert s["p50"] == 30.0

    def test_percentiles(self):
        h = Histogram("test")
        for v in range(1, 101):  # 1..100
            h.observe(float(v))
        s = h.stats()
        # Simple percentile via int(n * p) indices is approximate (±1 for 100 elems).
        assert s["p50"] in (50.0, 51.0)
        assert s["p95"] in (95.0, 96.0)
        assert s["p99"] in (99.0, 100.0)

    def test_unsorted_input(self):
        h = Histogram("test")
        for v in [100, 1, 50, 25, 75]:
            h.observe(v)
        s = h.stats()
        assert s["min"] == 1.0
        assert s["max"] == 100.0
        assert s["p50"] == 50.0


class TestMetricsRegistry:
    def test_counter_factory(self):
        r = MetricsRegistry()
        c = r.counter("hits")
        assert isinstance(c, Counter)
        assert c.name == "hits"

    def test_gauge_factory(self):
        r = MetricsRegistry()
        g = r.gauge("temp")
        assert isinstance(g, Gauge)

    def test_histogram_factory(self):
        r = MetricsRegistry()
        h = r.histogram("latency")
        assert isinstance(h, Histogram)

    def test_accessor_methods(self):
        r = MetricsRegistry()
        r.counter("c1")
        r.gauge("g1")
        r.histogram("h1")
        assert isinstance(r.get_counter("c1"), Counter)
        assert isinstance(r.get_gauge("g1"), Gauge)
        assert isinstance(r.get_histogram("h1"), Histogram)

    def test_attribute_access(self):
        r = MetricsRegistry()
        r.counter("my_counter")
        # Attribute access returns the Counter directly
        assert r.my_counter.get() == 0
        r.my_counter.inc()
        assert r.my_counter.get() == 1

    def test_attribute_access_missing(self):
        r = MetricsRegistry()
        with pytest.raises(AttributeError):
            _ = r.nonexistent

    def test_dir_lists_metrics(self):
        r = MetricsRegistry()
        r.counter("c1")
        r.gauge("g1")
        names = dir(r)
        assert "c1" in names
        assert "g1" in names

    def test_collect_all_counters(self):
        r = MetricsRegistry()
        r.counter("c1").inc(3)
        r.counter("c2").inc(7)
        snap = r.collect_all()
        assert snap.counters == {"c1": 3, "c2": 7}

    def test_collect_all_gauges(self):
        r = MetricsRegistry()
        r.gauge("g1").set(1.5)
        snap = r.collect_all()
        assert snap.gauges == {"g1": 1.5}

    def test_collect_all_histograms(self):
        r = MetricsRegistry()
        r.histogram("h1").observe(100.0)
        snap = r.collect_all()
        assert snap.histograms["h1"]["count"] == 1
        assert snap.histograms["h1"]["sum"] == 100.0

    def test_log_snapshot_does_not_raise(self):
        r = MetricsRegistry()
        r.counter("c1")
        r.log_snapshot()  # should not raise


class TestRegistryPreset:
    """Verify the module-level REGISTRY has all pre-defined metrics."""

    def test_all_preset_metrics_present(self):
        expected = [
            "llm_calls_total",
            "llm_calls_errors_total",
            "llm_latency_ms",
            "llm_tokens_total",
            "tool_calls_total",
            "tool_calls_errors_total",
            "tool_latency_ms",
            "agent_steps",
            "agent_errors_total",
            "active_sessions",
            "agent_stall_warnings_total",
        ]
        for name in expected:
            assert name in dir(REGISTRY), f"Missing preset metric: {name}"

    def test_preset_metrics_functional(self):
        REGISTRY.llm_calls_total.inc()
        REGISTRY.llm_latency_ms.observe(500.0)
        REGISTRY.active_sessions.set(3.0)
        snap = REGISTRY.collect_all()
        assert snap.counters["llm_calls_total"] >= 1
        assert snap.histograms["llm_latency_ms"]["count"] >= 1
        assert snap.gauges["active_sessions"] == 3.0


class TestMetricsRegistrySnapshot:
    def test_defaults(self):
        snap = MetricsRegistrySnapshot()
        assert snap.counters == {}
        assert snap.gauges == {}
        assert snap.histograms == {}

    def test_with_data(self):
        snap = MetricsRegistrySnapshot(
            counters={"a": 1},
            gauges={"b": 2.0},
            histograms={"c": {"count": 1, "sum": 100.0}},
        )
        assert snap.counters["a"] == 1
        assert snap.gauges["b"] == 2.0
