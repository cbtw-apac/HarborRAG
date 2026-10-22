"""Process-wide OpenTelemetry tracing bootstrap for HarborRAG entry points."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("harborrag.runtime.observability.tracing")

_OTLP_ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")


def bootstrap_tracing(service_name: str) -> TracerProvider:
    """Install one SDK tracer provider; export over OTLP/HTTP when an endpoint is set.

    Call from process entry points only. A repeated call returns the installed
    provider, because OpenTelemetry refuses to replace a global provider.
    """

    # The SDK is optional; import it only when an entry point opts into tracing.
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    installed = trace.get_tracer_provider()
    if isinstance(installed, TracerProvider):
        return installed
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    if any(os.environ.get(name) for name in _OTLP_ENDPOINT_VARS):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        logger.info("Tracing exports over OTLP service_name=%s", service_name)
    else:
        logger.info("Tracing enabled without an exporter service_name=%s", service_name)
    trace.set_tracer_provider(provider)
    return provider
