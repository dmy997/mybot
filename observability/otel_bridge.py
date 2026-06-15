"""OpenTelemetry bridge — mirrors custom tracer spans to OTel and exports via OTLP.

Hooks into the module-level :data:`observability.trace.tracer` singleton via
its ``_on_span_start`` / ``_on_span_end`` callbacks.  Kept entirely optional:
when the OTel SDK is unavailable or ``MYBOT_OTEL_ENABLED`` is not set, the
bridge is a no-op.

Quick start with Jaeger::

    docker run -d --name jaeger -p 16686:16686 -p 4317:4317 -p 4318:4318 \\
        jaegertracing/all-in-one

    MYBOT_OTEL_ENABLED=1 mybot          # spans appear at http://localhost:16686

Environment variables
---------------------
``MYBOT_OTEL_ENABLED``
    Set to ``1`` or ``true`` to activate the bridge.
``MYBOT_OTEL_ENDPOINT``
    OTLP HTTP endpoint.  Default: ``http://localhost:4318/v1/traces``.
``MYBOT_OTEL_SERVICE_NAME``
    Service name reported to the collector.  Default: ``mybot``.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from .trace import Span, Tracer

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def otel_available() -> bool:
    """Return ``True`` when all required OTel packages are installed."""
    try:
        from opentelemetry import trace  # noqa: F401, F811
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: F401
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class OTelBridge:
    """Mirror custom :class:`Tracer` spans to OpenTelemetry.

    Installs ``_on_span_start`` / ``_on_span_end`` hooks on *tracer* and
    exports corresponding OTel spans to the configured OTLP endpoint.

    Safe to construct even when OTel packages are missing — in that case
    :meth:`install` logs a warning and returns ``False``.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        service_name: str | None = None,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(
            "MYBOT_OTEL_ENDPOINT", "http://localhost:4318/v1/traces"
        )
        self._service_name = service_name or os.environ.get(
            "MYBOT_OTEL_SERVICE_NAME", "mybot"
        )
        self._installed = False
        self._otel_span_map: dict[str, Any] = {}  # custom span_id → OTel ReadableSpan

    # -- install ----------------------------------------------------------------

    def install(self, tracer: Tracer) -> bool:
        """Register hooks on *tracer* and initialise OTel SDK.

        Returns ``True`` on success, ``False`` when OTel packages are missing.
        """
        if self._installed:
            return True

        if not otel_available():
            logger.info("OpenTelemetry packages not installed; bridge disabled")
            return False

        self._setup_otel_sdk()
        tracer._on_span_start.append(self._on_span_start)
        tracer._on_span_end.append(self._on_span_end)
        self._installed = True
        logger.info(
            "OTel bridge installed — exporting traces to {} (service={})",
            self._endpoint,
            self._service_name,
        )
        return True

    # -- OTel SDK setup ----------------------------------------------------------

    def _setup_otel_sdk(self) -> None:
        """Initialise OTel TracerProvider with an OTLP HTTP exporter."""
        from opentelemetry import trace as otel_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({SERVICE_NAME: self._service_name})
        exporter = OTLPSpanExporter(endpoint=self._endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
        self._otel_tracer = otel_trace.get_tracer(self._service_name)

    # -- hooks ------------------------------------------------------------------

    def _on_span_start(self, span: Span) -> None:
        """Create a corresponding OTel span when a custom span starts."""
        from opentelemetry.trace import set_span_in_context

        # Build parent context so OTel can reconstruct the trace tree
        parent_ctx = None
        if span.context.parent_span_id:
            parent_otel = self._otel_span_map.get(span.context.parent_span_id)
            if parent_otel is not None:
                parent_ctx = set_span_in_context(parent_otel)

        otel_span = self._otel_tracer.start_span(
            span.name,
            attributes=span.attributes,
            context=parent_ctx,
        )
        self._otel_span_map[span.context.span_id] = otel_span

    def _on_span_end(self, span: Span) -> None:
        """End the corresponding OTel span."""
        otel_span = self._otel_span_map.pop(span.context.span_id, None)
        if otel_span is None:
            return

        # Sync final attributes — some may have been added after span start
        # (e.g. tokens_in, latency_ms added after LLM response)
        for key, value in span.attributes.items():
            otel_span.set_attribute(key, value)

        if span.status == "error":
            from opentelemetry.trace import Status, StatusCode
            otel_span.set_status(Status(StatusCode.ERROR))

        # Carry over span events as OTel events (copy to avoid mutating original)
        for ev in span.events:
            name = ev.get("name", "event")
            attrs = {k: v for k, v in ev.items() if k not in ("name", "timestamp")}
            otel_span.add_event(name, attributes=attrs)

        otel_span.end()


# ---------------------------------------------------------------------------
# Auto-install — called by orchestrator on startup when env var is set
# ---------------------------------------------------------------------------


def auto_install(tracer: Tracer) -> bool:
    """Install the OTel bridge on *tracer* when ``MYBOT_OTEL_ENABLED`` is set.

    Returns ``True`` when the bridge was installed, ``False`` otherwise.
    """
    env = os.environ.get("MYBOT_OTEL_ENABLED", "").strip().lower()
    if env not in ("1", "true", "yes"):
        return False

    bridge = OTelBridge()
    return bridge.install(tracer)
