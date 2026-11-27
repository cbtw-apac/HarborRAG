"""MCP tool-call and authentication telemetry: one span and a few metrics per call.

Labels stay low-cardinality (tool name, status, reason code), and nothing a caller sent
or a tool returned is recorded beyond counts, sizes and the tool's own reason codes.
Instruments come from the global OpenTelemetry providers, so they record nothing until
export is configured.

Typical usage example:

    with telemetry.tool_call("vector_search", tenant_id=tenant_id) as call:
        result = await run_tool()
        call.succeed(result, status=ToolCallStatus.OK)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from harborrag_core.observability.log_events import root_cause

from .span_failures import ERROR_TYPE_ATTRIBUTE, record_failure, record_failure_code

TOOL_ATTRIBUTE = "mcp.tool"
TENANT_ATTRIBUTE = "harborrag.tenant_id"
RESULT_LIST_KEYS = ("results", "candidates", "triplets", "paths", "relations")
"""Where a retrieval tool's results are, in the order looked up.

``vector_search`` returns ``results``, ``resolve_graph_nodes`` ``candidates``, the
triplet and path searches ``triplets`` and ``paths``. A subgraph counts its
``relations``, because its ``nodes`` always hold the start node.
"""
UNKNOWN_TENANT = "none"
ERROR_CODE_LABEL = "error_code"
OTHER_REASON = "other"
UNSPECIFIED_REASON = "unspecified"
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_REJECTED_COUNT_KEYS = {
    "stale_count": "stale",
    "stale_candidates": "stale",
    "unpublished_count": "unpublished",
}
_DURATION_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_RESULT_COUNT_BUCKETS = (0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
_RESPONSE_SIZE_BUCKETS_BYTES = (1_024.0, 4_096.0, 16_384.0, 65_536.0, 262_144.0, 1_048_576.0)


class ToolCallStatus(StrEnum):
    """How one MCP tool call ended; only ``ERROR`` counts against availability."""

    OK = "ok"
    TOOL_ERROR = "tool_error"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ERROR = "error"


class AuthFailureReason(StrEnum):
    """Why the MCP server refused a request's bearer token.

    ``REJECTED_BEARER`` covers unknown, revoked and expired keys, and a key store
    that could not be reached; the server's WARNING log tells them apart.
    """

    MISSING_BEARER = "missing_token"
    REJECTED_BEARER = "rejected_token"
    VERIFIER_ERROR = "verifier_error"
    ROLE_DENIED = "role_denied"
    TENANT_DENIED = "tenant_denied"


@dataclass(slots=True)
class ToolCallObservation:
    """The outcome of one tool call, filled in by the caller before the call ends.

    Attributes:
        status: How the call ended. Left at ``ERROR`` when the call raises.
        result: The tool's result object, when it returned one.
        error: The exception the call raised, when it raised one.
    """

    status: ToolCallStatus = ToolCallStatus.ERROR
    result: Mapping[str, object] | None = None
    error: BaseException | None = None

    def succeed(self, result: Mapping[str, object], *, status: ToolCallStatus) -> None:
        """Record the result the tool returned and how it ended."""
        self.result = result
        self.status = status


class McpTelemetry:
    """Record MCP tool calls and authentication refusals.

    Args:
        meter: Meter for the instruments. The global ``harborrag.mcp`` meter when omitted.
        tracer: Tracer for tool-call spans. The global ``harborrag.mcp`` tracer when omitted.
    """

    def __init__(
        self,
        *,
        meter: metrics.Meter | None = None,
        tracer: trace.Tracer | None = None,
    ) -> None:
        meter = meter or metrics.get_meter("harborrag.mcp")
        self._tracer = tracer or trace.get_tracer("harborrag.mcp")
        self._calls = meter.create_counter(
            "harborrag.mcp.tool.calls", unit="1", description="MCP tool calls by outcome."
        )
        self._duration = meter.create_histogram(
            "harborrag.mcp.tool.duration",
            unit="s",
            description="MCP tool call duration.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS_SECONDS,
        )
        self._results = meter.create_histogram(
            "harborrag.retrieval.results",
            unit="{result}",
            description="Results returned by a successful retrieval tool call.",
            explicit_bucket_boundaries_advisory=_RESULT_COUNT_BUCKETS,
        )
        self._response_size = meter.create_histogram(
            "harborrag.mcp.tool.response.size",
            unit="By",
            description="Serialized size of a successful tool result.",
            explicit_bucket_boundaries_advisory=_RESPONSE_SIZE_BUCKETS_BYTES,
        )
        self._auth_failures = meter.create_counter(
            "harborrag.mcp.auth.failures", unit="1", description="Refused MCP bearer tokens."
        )
        self._incomplete = meter.create_counter(
            "harborrag.mcp.tool.incomplete",
            unit="1",
            description="Successful tool calls whose answer is incomplete, by reason code.",
        )
        self._rejected = meter.create_counter(
            "harborrag.mcp.tool.rejected_candidates",
            unit="{candidate}",
            description="Candidates a tool dropped as stale or not yet published.",
        )

    @contextmanager
    def tool_call(self, tool: str, *, tenant_id: str | None) -> Iterator[ToolCallObservation]:
        """Trace one tool call and record its outcome when the block ends.

        The block runs inside the ``mcp.tool.<tool>`` span, so spans and logs it
        produces join the same trace. Only an ``ERROR`` outcome marks the span as
        failed, and exception messages are never recorded. Exceptions propagate.

        Args:
            tool: The tool's registered name.
            tenant_id: The tenant the call reads, when known. It labels the
                call's metrics, ``none`` when unknown.

        Yields:
            The observation the caller fills in before the block ends.
        """
        observation = ToolCallObservation()
        started = perf_counter()
        with self._tracer.start_as_current_span(
            f"mcp.tool.{tool}", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute(TOOL_ATTRIBUTE, tool)
            if tenant_id:
                span.set_attribute(TENANT_ATTRIBUTE, tenant_id)
            try:
                yield observation
            except BaseException as error:
                observation.error = error
                raise
            finally:
                labels = {"tool": tool, "tenant": tenant_id or UNKNOWN_TENANT}
                self._record(labels, observation, perf_counter() - started, span)

    def record_auth_failure(self, reason: AuthFailureReason) -> None:
        """Count one refused bearer token.

        Args:
            reason: Why the token was refused.
        """
        self._auth_failures.add(1, {"reason": reason.value})

    def _record(
        self,
        labels: dict[str, str],
        observation: ToolCallObservation,
        duration_seconds: float,
        span: trace.Span,
    ) -> None:
        """Record the call's counters and histograms, and mark the span with its outcome.

        A tool-reported failure carries the tool's ``error_code`` as a metric label and on
        the span, so a store outage and a disabled tool stay apart.
        """
        status = observation.status
        call_labels = {**labels, "status": status.value}
        if status is ToolCallStatus.TOOL_ERROR:
            call_labels[ERROR_CODE_LABEL] = _tool_error_code(observation.result)
        self._calls.add(1, call_labels)
        self._duration.record(duration_seconds, labels)
        span.set_attribute("mcp.tool.status", status.value)
        _mark_span(span, observation, call_labels.get(ERROR_CODE_LABEL))
        if status is not ToolCallStatus.OK or observation.result is None:
            return
        result = observation.result
        self._response_size.record(_serialized_size(result), labels)
        result_count = _result_count(result)
        if result_count is not None:
            self._results.record(result_count, labels)
            span.set_attribute("mcp.tool.result_count", result_count)
        for reason in _incomplete_reasons(result):
            self._incomplete.add(1, {**labels, "reason": reason})
        for reason, count in _rejected_counts(result).items():
            self._rejected.add(count, {**labels, "reason": reason})


@cache
def shared_mcp_telemetry() -> McpTelemetry:
    """Return the process's MCP telemetry, created on first use.

    Every MCP server in a process shares one set of instruments, because the
    in-process facade builds a server per call.
    """
    return McpTelemetry()


def _mark_span(span: trace.Span, observation: ToolCallObservation, code: str | None) -> None:
    """Mark a failed call's span with its cause; a refusal only names the error type."""
    if observation.status is ToolCallStatus.ERROR:
        if observation.error is not None:
            record_failure(span, observation.error)
        else:
            span.set_status(Status(StatusCode.ERROR))
    elif observation.status is ToolCallStatus.TOOL_ERROR:
        record_failure_code(span, code or UNSPECIFIED_REASON)
    elif observation.status is ToolCallStatus.REFUSED and observation.error is not None:
        span.set_attribute(ERROR_TYPE_ATTRIBUTE, type(root_cause(observation.error)).__name__)


def _tool_error_code(result: Mapping[str, object] | None) -> str:
    """Return the code a tool put on its failure, ``unspecified`` or ``other`` when unusable."""
    code = result.get(ERROR_CODE_LABEL) if result is not None else None
    if not isinstance(code, str):
        return UNSPECIFIED_REASON
    return code if _REASON_CODE.fullmatch(code) else OTHER_REASON


def _serialized_size(result: Mapping[str, object]) -> int:
    """Return the size in bytes of ``result`` as compact JSON."""
    return len(json.dumps(result, separators=(",", ":"), default=str).encode())


def _result_count(result: Mapping[str, object]) -> int | None:
    """Return how many results a retrieval tool returned, or None for other tools."""
    for key in RESULT_LIST_KEYS:
        found = result.get(key)
        if isinstance(found, list):
            return len(found)
    return None


def _incomplete_reasons(result: Mapping[str, object]) -> list[str]:
    """Return the reason codes of an answer its ``completion`` marks incomplete.

    A reason that is not a short lower-case code is reported as ``other``, so a
    tool can never put free text into a metric label.
    """
    completion = result.get("completion")
    if not isinstance(completion, Mapping) or completion.get("complete") is not False:
        return []
    reasons = completion.get("reasons")
    codes = [
        reason if isinstance(reason, str) and _REASON_CODE.fullmatch(reason) else OTHER_REASON
        for reason in (reasons if isinstance(reasons, list) else [])
    ]
    return sorted(set(codes)) or [UNSPECIFIED_REASON]


def _rejected_counts(result: Mapping[str, object]) -> dict[str, int]:
    """Return the stale and unpublished candidates a tool's ``diagnostics`` report."""
    diagnostics = result.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key, reason in _REJECTED_COUNT_KEYS.items():
        value = diagnostics.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            counts[reason] = counts.get(reason, 0) + value
    return counts
