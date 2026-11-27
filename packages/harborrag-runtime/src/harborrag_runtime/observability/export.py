"""OpenTelemetry export of traces, metrics and logs for one HarborRAG process.

Nothing is installed unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so a process started
without the observability stack creates no exporter, sends nothing and logs nothing about
telemetry. Only long-running process entry points call it, after ``configure_logging``.
Nothing here logs from a handler, a filter or an exporter: they run for every record, so a
log written there would feed itself.

Typical usage example:

    configure_logging()
    configure_telemetry_export(TelemetryService.API)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from opentelemetry import trace

from harborrag_core.observability.log_events import log_event
from harborrag_core.observability.process_logging import (
    ROOT_LOGGER_NAME,
    SPAN_ID_FIELD,
    TRACE_ID_FIELD,
)
from harborrag_runtime.errors import MissingOptionalDependencyError

if TYPE_CHECKING:
    from opentelemetry.sdk.resources import Resource

ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
SDK_DISABLED_ENV_VAR = "OTEL_SDK_DISABLED"
METRIC_EXPORT_INTERVAL_ENV_VAR = "OTEL_METRIC_EXPORT_INTERVAL"
ENVIRONMENT_ENV_VAR = "HARBORRAG_ENV"
DEFAULT_METRIC_EXPORT_INTERVAL_MS = 15_000
DEPLOYMENT_ENVIRONMENT = "deployment.environment"

logger = logging.getLogger("harborrag.runtime.observability.export")


class TelemetryService(StrEnum):
    """The ``service.name`` each HarborRAG process reports."""

    API = "harborrag-api"
    MCP = "harborrag-mcp"
    MCP_UI = "harborrag-mcp-ui"
    WORKER = "harborrag-worker"


_SERVICE_DISTRIBUTIONS = {
    TelemetryService.API: "harborrag-app",
    TelemetryService.MCP: "harborrag-mcp-server",
    TelemetryService.MCP_UI: "harborrag-mcp-server",
    TelemetryService.WORKER: "harborrag-runtime",
}


class TraceContextFilter(logging.Filter):
    """Copy the active span's trace and span IDs onto each log record.

    The console formatter prints them, so a log line can be matched to its trace.
    A record logged outside any recorded span is left unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add ``trace_id`` and ``span_id`` when a recorded span is active."""
        context = trace.get_current_span().get_span_context()
        if context.is_valid and not hasattr(record, TRACE_ID_FIELD):
            setattr(record, TRACE_ID_FIELD, trace.format_trace_id(context.trace_id))
            setattr(record, SPAN_ID_FIELD, trace.format_span_id(context.span_id))
        return True


def export_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether an OTLP endpoint is configured and the SDK is not disabled.

    Args:
        environ: The environment to read. The process environment when omitted.

    Returns:
        True when telemetry should be exported.
    """
    values = os.environ if environ is None else environ
    if values.get(SDK_DISABLED_ENV_VAR, "").strip().lower() == "true":
        return False
    return bool(values.get(ENDPOINT_ENV_VAR, "").strip())


def configure_telemetry_export(
    service: TelemetryService,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Install OTLP/HTTP export of traces, metrics and logs for this process.

    The exporters read the endpoint, headers and timeouts from the standard
    ``OTEL_EXPORTER_OTLP_*`` variables. ``harborrag`` log records gain the active
    trace and span IDs on the console and are also exported. A repeated call, or a
    call after another library installed an SDK provider, keeps the installed one.

    Args:
        service: The service name this process reports. Its package's installed
            version becomes ``service.version``.
        environ: The environment to read. The process environment when omitted.

    Returns:
        True when export is configured, False when no endpoint is set.

    Raises:
        MissingOptionalDependencyError: An endpoint is set but the OpenTelemetry SDK
            or its OTLP exporter is not installed.
    """
    values = os.environ if environ is None else environ
    if not export_enabled(values):
        return False
    try:
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            f"{ENDPOINT_ENV_VAR} is set but OpenTelemetry export requires "
            "harborrag-adapters[opentelemetry]"
        ) from exc
    attributes = {SERVICE_NAME: service.value, SERVICE_VERSION: _service_version(service)}
    environment = values.get(ENVIRONMENT_ENV_VAR, "").strip()
    if environment:
        attributes[DEPLOYMENT_ENVIRONMENT] = environment
    resource = Resource.create(attributes)
    _install_tracing(resource)
    _install_metrics(resource, values)
    _install_logs(resource)
    log_event(logger, "observability.export_configured", service_kind=service.value)
    return True


def _service_version(service: TelemetryService) -> str:
    """Return the installed version of the package that runs ``service``."""
    try:
        return version(_SERVICE_DISTRIBUTIONS[service])
    except PackageNotFoundError:
        return "0+unknown"


def _install_tracing(resource: Resource) -> None:
    """Install a batching OTLP span exporter unless an SDK tracer provider exists."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def _install_metrics(resource: Resource, values: Mapping[str, str]) -> None:
    """Install a periodic OTLP metric exporter unless an SDK meter provider exists."""
    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    if isinstance(metrics.get_meter_provider(), MeterProvider):
        return
    interval = (
        None if values.get(METRIC_EXPORT_INTERVAL_ENV_VAR) else DEFAULT_METRIC_EXPORT_INTERVAL_MS
    )
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))


def _install_logs(resource: Resource) -> None:
    """Export ``harborrag`` log records and stamp console lines with their trace IDs."""
    from opentelemetry._logs import get_logger_provider, set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    root = logging.getLogger(ROOT_LOGGER_NAME)
    if isinstance(get_logger_provider(), LoggerProvider):
        return
    for handler in root.handlers:
        handler.addFilter(TraceContextFilter())
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(provider)
    root.addHandler(LoggingHandler(level=root.level, logger_provider=provider))
