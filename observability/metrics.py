"""Lightweight in-memory metrics registry.

Provides Counter, Gauge, and Histogram metric types plus a module-level
``REGISTRY`` singleton pre-loaded with agent-relevant metrics.

Usage::

    from observability.metrics import REGISTRY
    REGISTRY.llm_calls_total.inc()
    REGISTRY.llm_latency_ms.observe(1234)
"""

from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


class Counter:
    """A monotonically-increasing counter (thread-safe)."""

    def __init__(self, name: str, description: str = "", unit: str = "") -> None:
        self.name = name
        self.description = description
        self.unit = unit
        self._value: int = 0
        self._lock = threading.Lock()

    def inc(self, delta: int = 1) -> None:
        with self._lock:
            self._value += delta

    def get(self) -> int:
        with self._lock:
            return self._value


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


class Gauge:
    """A value that can go up or down (thread-safe)."""

    def __init__(self, name: str, description: str = "", unit: str = "") -> None:
        self.name = name
        self.description = description
        self.unit = unit
        self._value: float = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, delta: float = 1.0) -> None:
        with self._lock:
            self._value += delta

    def dec(self, delta: float = 1.0) -> None:
        with self._lock:
            self._value -= delta

    def get(self) -> float:
        with self._lock:
            return self._value


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


class Histogram:
    """A distribution of observed values with percentile access (thread-safe)."""

    def __init__(self, name: str, description: str = "", unit: str = "") -> None:
        self.name = name
        self.description = description
        self.unit = unit
        self._values: list[float] = []
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._values.append(value)

    def stats(self) -> dict[str, float]:
        """Return ``{count, sum, min, max, avg, p50, p95, p99}``."""
        with self._lock:
            if not self._values:
                return {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0,
                        "p50": 0.0, "p95": 0.0, "p99": 0.0}
            sv = sorted(self._values)
            n = len(sv)
            return {
                "count": n,
                "sum": sum(sv),
                "min": sv[0],
                "max": sv[-1],
                "avg": sum(sv) / n,
                "p50": sv[n // 2],
                "p95": sv[int(n * 0.95)],
                "p99": sv[int(n * 0.99)],
            }


# ---------------------------------------------------------------------------
# Metrics registry
# ---------------------------------------------------------------------------


@dataclass
class MetricsRegistrySnapshot:
    counters: dict[str, int] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, dict[str, float]] = field(default_factory=dict)


class MetricsRegistry:
    """A named collection of Counter / Gauge / Histogram instances."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}

    # -- factory methods -------------------------------------------------------

    def counter(self, name: str, *, description: str = "", unit: str = "") -> Counter:
        c = Counter(name, description=description, unit=unit)
        self._counters[name] = c
        return c

    def gauge(self, name: str, *, description: str = "", unit: str = "") -> Gauge:
        g = Gauge(name, description=description, unit=unit)
        self._gauges[name] = g
        return g

    def histogram(self, name: str, *, description: str = "", unit: str = "") -> Histogram:
        h = Histogram(name, description=description, unit=unit)
        self._histograms[name] = h
        return h

    # -- accessors -------------------------------------------------------------

    def get_counter(self, name: str) -> Counter:
        return self._counters[name]

    def get_gauge(self, name: str) -> Gauge:
        return self._gauges[name]

    def get_histogram(self, name: str) -> Histogram:
        return self._histograms[name]

    # -- snapshot --------------------------------------------------------------

    def collect_all(self) -> MetricsRegistrySnapshot:
        """Return a consistent snapshot of all metric values."""
        return MetricsRegistrySnapshot(
            counters={n: c.get() for n, c in self._counters.items()},
            gauges={n: g.get() for n, g in self._gauges.items()},
            histograms={n: h.stats() for n, h in self._histograms.items()},
        )

    def log_snapshot(self) -> None:
        """Log current metric values via loguru (structured)."""
        from loguru import logger

        snap = self.collect_all()
        logger.bind(
            event_type="MetricsSnapshot",
            counters=snap.counters,
            gauges=snap.gauges,
            histograms=snap.histograms,
        ).info("Metrics snapshot")

    # -- attribute-style access --------------------------------------------------
    # Supports: REGISTRY.llm_calls_total, REGISTRY.llm_latency_ms, etc.

    def __getattr__(self, name: str) -> Any:
        if name in self._counters:
            return self._counters[name]
        if name in self._gauges:
            return self._gauges[name]
        if name in self._histograms:
            return self._histograms[name]
        raise AttributeError(f"No metric named {name!r}")

    def __dir__(self) -> list[str]:
        return list(self._counters) + list(self._gauges) + list(self._histograms)


# ---------------------------------------------------------------------------
# Pre-defined agent metrics
# ---------------------------------------------------------------------------


REGISTRY = MetricsRegistry()

REGISTRY.counter("llm_calls_total", description="Total number of LLM calls")
REGISTRY.counter("llm_calls_errors_total", description="Total number of failed LLM calls")
REGISTRY.histogram("llm_latency_ms", description="LLM call latency in milliseconds")
REGISTRY.counter("llm_tokens_total", description="Total tokens consumed")
REGISTRY.counter("tool_calls_total", description="Total number of tool calls")
REGISTRY.counter("tool_calls_errors_total", description="Total number of failed tool calls")
REGISTRY.histogram("tool_latency_ms", description="Tool execution latency in milliseconds")
REGISTRY.histogram("agent_steps", description="Steps per agent run")
REGISTRY.counter("agent_errors_total", description="Total number of agent error exits")
REGISTRY.gauge("active_sessions", description="Number of sessions in memory")
REGISTRY.counter("agent_stall_warnings_total", description="Stall detection warnings")

# -- persistence -----------------------------------------------------------

_save_timer: threading.Timer | None = None
_save_interval = 60  # seconds between auto-saves


def _get_persistence_store():
    """Lazy-import to avoid circular dependency at module load time."""
    from observability.persistence import store
    return store


def restore_metrics() -> int:
    """Restore counter/gauge values from disk.  Returns number of restored metrics."""
    obs_store = _get_persistence_store()
    if obs_store is None:
        return 0
    data = obs_store.load_metrics()
    if data is None:
        return 0
    n = 0
    for name, value in data.get("counters", {}).items():
        c = REGISTRY._counters.get(name)
        if c is not None:
            with c._lock:
                c._value = max(c._value, int(value))
            n += 1
    for name, value in data.get("gauges", {}).items():
        g = REGISTRY._gauges.get(name)
        if g is not None:
            with g._lock:
                g._value = float(value)
            n += 1
    logger.info("Restored {} metrics from disk", n)
    return n


def _save_metrics() -> None:
    obs_store = _get_persistence_store()
    if obs_store is None:
        return
    snap = REGISTRY.collect_all()
    obs_store.save_metrics({
        "counters": snap.counters,
        "gauges": snap.gauges,
    })


def _schedule_auto_save() -> None:
    global _save_timer
    _save_metrics()
    _save_timer = threading.Timer(_save_interval, _schedule_auto_save)
    _save_timer.daemon = True
    _save_timer.start()


def start_metrics_persistence() -> None:
    """Start periodic metrics auto-save (every 60 s) and register exit hook."""
    _schedule_auto_save()
    atexit.register(_save_metrics)
