"""Telemetry the observability stack reads: MCP and chat metrics, spans, and export setup."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, generate_latest

from harborrag_adapters.connectors.policies.network import ResponseObservation
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.observability.process_logging import configure_logging
from harborrag_engine.retrieval.service.contracts import RetrievalStep
from harborrag_runtime.observability.chat_telemetry import (
    ChatTelemetry,
    ChatTurnStatus,
    CitationCheck,
)
from harborrag_runtime.observability.export import (
    ENDPOINT_ENV_VAR,
    SDK_DISABLED_ENV_VAR,
    TelemetryService,
    TraceContextFilter,
    configure_telemetry_export,
    export_enabled,
)
from harborrag_runtime.observability.http_tracing import server_span
from harborrag_runtime.observability.mcp_telemetry import (
    AuthFailureReason,
    McpTelemetry,
    ToolCallStatus,
)
from harborrag_runtime.observability.model import IngestionTelemetry
from harborrag_runtime.observability.observability_types import IngestionStage
from harborrag_runtime.observability.parse_timing import TimedDocumentParser
from harborrag_runtime.observability.span_failures import record_failure
from harborrag_runtime.observability.stage_observation import StageObservation
from harborrag_runtime.observability.workflow_trace import workflow_trace

pytestmark = pytest.mark.unit

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(spans: InMemorySpanExporter) -> trace.Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    return provider.get_tracer("test")


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    """Return every recorded data point, keyed by instrument name."""
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        metric.name: list(metric.data.data_points)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_export_is_off_without_an_endpoint_and_when_the_sdk_is_disabled() -> None:
    assert export_enabled({}) is False
    assert export_enabled({ENDPOINT_ENV_VAR: "  "}) is False
    assert export_enabled({ENDPOINT_ENV_VAR: "http://collector:4318"}) is True
    assert (
        export_enabled({ENDPOINT_ENV_VAR: "http://collector:4318", SDK_DISABLED_ENV_VAR: "true"})
        is False
    )
    assert configure_telemetry_export(TelemetryService.API, environ={}) is False


def test_export_installs_each_provider_once_with_the_service_identity() -> None:
    script = (
        "from opentelemetry import metrics, trace\n"
        "from opentelemetry._logs import get_logger_provider\n"
        "from harborrag_core.observability.process_logging import configure_logging\n"
        "from harborrag_runtime.observability.export import "
        "TelemetryService, configure_telemetry_export\n"
        "configure_logging()\n"
        "env = {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://127.0.0.1:9', 'HARBORRAG_ENV': 'test'}\n"
        "assert configure_telemetry_export(TelemetryService.MCP, environ=env)\n"
        "provider = trace.get_tracer_provider()\n"
        "assert configure_telemetry_export(TelemetryService.MCP, environ=env)\n"
        "assert trace.get_tracer_provider() is provider\n"
        "attributes = provider.resource.attributes\n"
        "assert attributes['service.name'] == 'harborrag-mcp', attributes\n"
        "assert attributes['deployment.environment'] == 'test', attributes\n"
        "assert type(metrics.get_meter_provider()).__name__ == 'MeterProvider'\n"
        "assert type(get_logger_provider()).__name__ == 'LoggerProvider'\n"
    )
    completed = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr


def test_console_lines_carry_the_active_trace_and_span_ids(tracer: trace.Tracer) -> None:
    stream = io.StringIO()
    logger = configure_logging("INFO", stream=stream)
    handler = logger.handlers[-1]
    trace_filter = TraceContextFilter()
    handler.addFilter(trace_filter)
    try:
        with tracer.start_as_current_span("request") as span:
            logging.getLogger("harborrag.test").info("inside")
        logging.getLogger("harborrag.test").info("outside")
    finally:
        handler.removeFilter(trace_filter)

    inside, outside = stream.getvalue().splitlines()[-2:]
    trace_id = trace.format_trace_id(span.get_span_context().trace_id)
    assert inside.endswith(
        f"trace_id={trace_id} span_id={trace.format_span_id(span.get_span_context().span_id)}"
    )
    assert "trace_id=" not in outside


def test_a_successful_tool_call_counts_its_results_size_and_duration(
    reader: InMemoryMetricReader, tracer: trace.Tracer, spans: InMemorySpanExporter
) -> None:
    telemetry = McpTelemetry(
        meter=MeterProvider(metric_readers=[reader]).get_meter("t"), tracer=tracer
    )

    with telemetry.tool_call("vector_search", tenant_id="tenant-a") as call:
        call.succeed({"ok": True, "results": [{"id": 1}, {"id": 2}]}, status=ToolCallStatus.OK)

    points = _points(reader)
    assert [dict(p.attributes) for p in points["harborrag.mcp.tool.calls"]] == [
        {"tool": "vector_search", "tenant": "tenant-a", "status": "ok"}
    ]
    assert points["harborrag.retrieval.results"][0].sum == 2
    assert points["harborrag.mcp.tool.response.size"][0].count == 1
    assert points["harborrag.mcp.tool.duration"][0].count == 1
    (span,) = spans.get_finished_spans()
    assert span.name == "mcp.tool.vector_search"
    assert span.attributes is not None
    assert span.attributes["harborrag.tenant_id"] == "tenant-a"
    assert span.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(
    ("tool", "result", "count"),
    [
        ("resolve_graph_nodes", {"ok": True, "candidates": [{"node_key": "n1"}]}, 1),
        ("graph_triplet_search", {"ok": True, "triplets": []}, 0),
        ("graph_path_search", {"ok": True, "paths": [{}, {}]}, 2),
        (
            "graph_subgraph_search",
            {"ok": True, "nodes": [{"node_key": "start"}], "relations": []},
            0,
        ),
    ],
)
def test_graph_tool_results_are_counted_like_vector_results(
    reader: InMemoryMetricReader, tool: str, result: dict[str, object], count: int
) -> None:
    telemetry = McpTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))

    with telemetry.tool_call(tool, tenant_id="tenant-a") as call:
        call.succeed(result, status=ToolCallStatus.OK)

    (point,) = _points(reader)["harborrag.retrieval.results"]
    assert dict(point.attributes) == {"tool": tool, "tenant": "tenant-a"}
    assert point.sum == count


def test_an_incomplete_graph_answer_counts_its_reasons_and_dropped_candidates(
    reader: InMemoryMetricReader,
) -> None:
    telemetry = McpTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    truncated = {
        "ok": True,
        "nodes": [],
        "relations": [{}, {}],
        "diagnostics": {"stale_count": 3, "unpublished_count": 1, "accepted_count": 2},
        "completion": {"complete": False, "reasons": ["max_nodes", "Alex Example's title"]},
    }

    with telemetry.tool_call("graph_subgraph_search", tenant_id="tenant-a") as call:
        call.succeed(truncated, status=ToolCallStatus.OK)
    with telemetry.tool_call("graph_path_search", tenant_id="tenant-a") as call:
        call.succeed(
            {"ok": True, "paths": [], "completion": {"complete": True, "reasons": []}},
            status=ToolCallStatus.OK,
        )

    points = _points(reader)
    reasons = {p.attributes["reason"]: p.value for p in points["harborrag.mcp.tool.incomplete"]}
    assert reasons == {"max_nodes": 1, "other": 1}
    rejected = {
        p.attributes["reason"]: p.value for p in points["harborrag.mcp.tool.rejected_candidates"]
    }
    assert rejected == {"stale": 3, "unpublished": 1}


def test_a_refused_call_is_not_an_error_but_a_failure_is(
    reader: InMemoryMetricReader, tracer: trace.Tracer, spans: InMemorySpanExporter
) -> None:
    telemetry = McpTelemetry(
        meter=MeterProvider(metric_readers=[reader]).get_meter("t"), tracer=tracer
    )

    with (
        pytest.raises(PermissionError),
        telemetry.tool_call("graph_search", tenant_id=None) as call,
    ):
        call.status = ToolCallStatus.REFUSED
        raise PermissionError("tool disabled")
    with pytest.raises(RuntimeError), telemetry.tool_call("graph_search", tenant_id=None):
        raise RuntimeError("store down")
    telemetry.record_auth_failure(AuthFailureReason.REJECTED_BEARER)

    points = _points(reader)
    statuses = {p.attributes["status"]: p.value for p in points["harborrag.mcp.tool.calls"]}
    assert statuses == {"refused": 1, "error": 1}
    assert "harborrag.retrieval.results" not in points
    assert [dict(p.attributes) for p in points["harborrag.mcp.auth.failures"]] == [
        {"reason": "rejected_token"}
    ]
    refused, failed = spans.get_finished_spans()
    assert refused.status.status_code is StatusCode.UNSET
    assert (refused.attributes or {})["error.type"] == "PermissionError"
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.status.description == "RuntimeError"
    assert [dict(event.attributes or {}) for event in failed.events] == [
        {"exception.type": "builtins.RuntimeError"}
    ]


def test_a_tool_reported_failure_carries_its_code_on_the_metric_and_the_span(
    reader: InMemoryMetricReader, tracer: trace.Tracer, spans: InMemorySpanExporter
) -> None:
    telemetry = McpTelemetry(
        meter=MeterProvider(metric_readers=[reader]).get_meter("t"), tracer=tracer
    )

    with telemetry.tool_call("vector_search", tenant_id="tenant-a") as call:
        call.succeed(
            {
                "ok": False,
                "error": "vector retrieval backend failed",
                "error_code": "backend_unavailable",
            },
            status=ToolCallStatus.TOOL_ERROR,
        )

    (point,) = _points(reader)["harborrag.mcp.tool.calls"]
    assert dict(point.attributes) == {
        "tool": "vector_search",
        "tenant": "tenant-a",
        "status": "tool_error",
        "error_code": "backend_unavailable",
    }
    (span,) = spans.get_finished_spans()
    assert (span.status.status_code, span.status.description) == (
        StatusCode.ERROR,
        "backend_unavailable",
    )
    assert (span.attributes or {})["harborrag.error.code"] == "backend_unavailable"


def test_a_chat_turn_is_recorded_once_with_its_time_to_answer(
    reader: InMemoryMetricReader,
) -> None:
    telemetry = ChatTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))

    answered = telemetry.start_turn("instant", tenant_id="tenant-a")
    answered.answered()
    answered.finish(ChatTurnStatus.OK)
    answered.finish(ChatTurnStatus.ERROR)
    refused = telemetry.start_turn("classic", tenant_id="tenant-a")
    refused.finish(ChatTurnStatus.REFUSED)
    telemetry.record_citation_check(CitationCheck.PARTIAL)

    points = _points(reader)
    turns = {
        (p.attributes["mode"], p.attributes["status"]): p.value
        for p in points["harborrag.chat.turns"]
    }
    assert turns == {("instant", "ok"): 1, ("classic", "refused"): 1}
    assert [dict(p.attributes) for p in points["harborrag.chat.ttft"]] == [
        {"mode": "instant", "tenant": "tenant-a"}
    ]
    assert len(points["harborrag.chat.turn.duration"]) == 2
    assert [dict(p.attributes) for p in points["harborrag.chat.citation.checks"]] == [
        {"result": "partial"}
    ]


def test_every_activity_of_one_workflow_run_shares_one_trace() -> None:
    def current_trace_id() -> int:
        return trace.get_current_span().get_span_context().trace_id

    with workflow_trace("document-1", "run-1"):
        first = current_trace_id()
    with workflow_trace("document-1", "run-1"):
        again = current_trace_id()
    with workflow_trace("document-1", "run-2"):
        retried_run = current_trace_id()

    assert first == again != retried_run
    assert current_trace_id() == 0


def test_a_request_span_continues_the_callers_trace() -> None:
    with server_span("POST", "/v1/chat/completions", {"traceparent": TRACEPARENT}) as span:
        trace_id = trace.get_current_span().get_span_context().trace_id
        span.finish(200)

    assert trace.format_trace_id(trace_id) == TRACEPARENT.split("-")[1]


class _FixedParser:
    def parse(self, source: object, /) -> ParsedDocument:
        return ParsedDocument(content="body", parser_name="pdf-liteparse")


@pytest.fixture
def ingestion() -> Iterator[tuple[IngestionTelemetry, CollectorRegistry]]:
    registry = CollectorRegistry()
    yield IngestionTelemetry(registry=registry), registry


def _observation(status_code: int | None) -> ResponseObservation:
    return ResponseObservation(
        connector="jira", method="GET", host="example.atlassian.net", status_code=status_code
    )


def test_a_failed_connector_request_becomes_an_event_on_the_active_span(
    ingestion: tuple[IngestionTelemetry, CollectorRegistry],
    tracer: trace.Tracer,
    spans: InMemorySpanExporter,
) -> None:
    telemetry, _ = ingestion

    with tracer.start_as_current_span("harborrag.ingestion.discovery"):
        telemetry.record_connector_response(_observation(200))
        telemetry.record_connector_response(_observation(401))

    (span,) = spans.get_finished_spans()
    (event,) = span.events
    assert event.name == "connector.response"
    assert dict(event.attributes or {}) == {
        "harborrag.connector": "jira",
        "http.request.method": "GET",
        "server.address": "example.atlassian.net",
        "http.response.status_code": 401,
    }


def test_a_failure_names_its_cause_without_the_exception_text(
    tracer: trace.Tracer, spans: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("retrieval.vector.query") as span:
        record_failure(span, LookupError("collection for Alex Example does not exist"))

    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.status.description == "LookupError"
    assert (finished.attributes or {})["error.type"] == "LookupError"
    assert [dict(event.attributes or {}) for event in finished.events] == [
        {"exception.type": "builtins.LookupError"}
    ]


class _StageTelemetry:
    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def record_activity_retry(self, stage: IngestionStage) -> None:
        return None

    def record_stage(self, stage: IngestionStage, outcome: str, duration: float) -> None:
        return None

    def stage_tracer(self) -> trace.Tracer:
        return self._tracer


class _SourceRejected(Exception):
    status_code = 401


def test_a_stage_span_keeps_the_classified_cause_when_the_error_is_wrapped(
    tracer: trace.Tracer, spans: InMemorySpanExporter
) -> None:
    observation = StageObservation(
        telemetry=_StageTelemetry(tracer), stage=IngestionStage.DISCOVERY, attempt=1, attributes={}
    )

    with pytest.raises(RuntimeError), observation:
        error = _SourceRejected("token rejected")
        observation.fail(error, code="authentication_failed", retryable=False)
        raise RuntimeError("ingestion activity failed") from None

    (span,) = spans.get_finished_spans()
    attributes = span.attributes or {}
    assert span.status.description == "_SourceRejected: authentication_failed"
    assert (attributes["error.type"], attributes["harborrag.error.code"]) == (
        "_SourceRejected",
        "authentication_failed",
    )
    assert (attributes["http.response.status_code"], attributes["harborrag.error.retryable"]) == (
        401,
        False,
    )


def test_parse_time_connector_status_and_retrieval_steps_are_recorded(
    ingestion: tuple[IngestionTelemetry, CollectorRegistry],
) -> None:
    telemetry, registry = ingestion

    TimedDocumentParser(_FixedParser(), telemetry).parse(object())
    for status in (200, 204, 429, 503, None):
        telemetry.record_connector_response(_observation(status))
    with telemetry.retrieval_step(RetrievalStep.VECTOR_QUERY):
        pass

    exposition = generate_latest(registry).decode()
    assert (
        'harborrag_ingestion_parse_duration_seconds_count{parser="pdf-liteparse"} 1.0' in exposition
    )
    for status, count in (("2xx", 2), ("429", 1), ("5xx", 1), ("error", 1)):
        assert (
            f'harborrag_ingestion_connector_requests_total{{connector_type="jira",status="{status}"}} '
            f"{float(count)}"
        ) in exposition
