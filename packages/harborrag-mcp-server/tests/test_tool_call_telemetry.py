"""Every MCP tool call is counted by outcome; only a server-side failure is an error."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from harborrag_engine.tools.base import BaseTool, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.server import McpServer
from harborrag_runtime.observability.mcp_telemetry import McpTelemetry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _ResultTool(BaseTool):
    spec = ToolSpec("found", "Return two results.", output_schema={"type": "object"})

    async def call(self, arguments, *, principal_id):
        return {"ok": True, "results": [{"id": "a"}, {"id": "b"}]}


class _ReportedErrorTool(BaseTool):
    spec = ToolSpec("reported", "Report its own failure.", output_schema={"type": "object"})

    async def call(self, arguments, *, principal_id):
        return {"ok": False, "error": {"code": "not_found"}}


class _CrashingTool(BaseTool):
    spec = ToolSpec("crashing", "Fail inside the server.", output_schema={"type": "object"})

    async def call(self, arguments, *, principal_id):
        raise RuntimeError("store unavailable")


async def test_call_tool_counts_each_call_by_how_it_ended() -> None:
    reader = InMemoryMetricReader()
    server = McpServer(
        tools=[_ResultTool(), _ReportedErrorTool(), _CrashingTool()],
        audit=McpAuditLog(),
        telemetry=McpTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("test")),
    )

    await server.call_tool("found")
    await server.call_tool("reported")
    with pytest.raises(RuntimeError):
        await server.call_tool("crashing")
    with pytest.raises(ValueError, match="Unknown MCP tool"):
        await server.call_tool("missing")

    data = reader.get_metrics_data()
    assert data is not None
    metrics = {
        metric.name: metric.data.data_points
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    calls = {
        (point.attributes["tool"], point.attributes["status"]): point.value
        for point in metrics["harborrag.mcp.tool.calls"]
    }
    assert calls == {
        ("found", "ok"): 1,
        ("reported", "tool_error"): 1,
        ("crashing", "error"): 1,
        ("missing", "refused"): 1,
    }
    (results,) = metrics["harborrag.retrieval.results"]
    assert dict(results.attributes) == {"tool": "found", "tenant": "none"}
    assert results.sum == 2
