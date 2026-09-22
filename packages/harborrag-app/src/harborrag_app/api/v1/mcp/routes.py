"""MCP telemetry endpoints: status, usage by client/tool, recent queries, and config.

Read-only throughout (ML4-P3 plan §5.5): this slice reports usage recorded
by harborrag-mcp-server via the runtime bridge
(``harborrag_runtime.mcp_telemetry``); it does not itself talk to the MCP
server process. ``role reader`` everywhere except ``/clients`` and
``/queries``: those two surface a per-caller ``client`` identifier over data
that has no tenant dimension at all, so they require operator-wide scope
(``require_operator_role``), not just role rank.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.dependencies import require_operator_role, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.errors import HarborCapabilityError, HarborConnectionError

from .dependencies import McpTelemetryServiceDependency
from .range import RangeQuery, range_start
from .schemas import (
    McpClientListResponse,
    McpClientUsageResponse,
    McpConfigResponse,
    McpQueryEntryResponse,
    McpQueryListResponse,
    McpStatusResponse,
    McpToolListResponse,
    McpToolUsageResponse,
)

router = APIRouter(
    prefix="/mcp",
    tags=["MCP"],
    dependencies=[Depends(require_api_capacity)],
)

CONFIG_ERROR_RESPONSES = documented_error_responses(
    {501: "No MCP server has published a configuration snapshot yet"}
)


@router.get("/status", response_model=McpStatusResponse)
async def mcp_status(
    service: McpTelemetryServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> McpStatusResponse:
    del principal
    response = await service.mcp_status()
    return McpStatusResponse.model_validate(response.data)


@router.get("/clients", response_model=McpClientListResponse)
async def mcp_clients(
    service: McpTelemetryServiceDependency,
    # McpClientUsage has no tenant dimension: it is platform-wide, so this
    # route requires operator-wide scope, not just the "reader" role.
    principal: Annotated[Principal, Depends(require_operator_role("admin"))],
) -> McpClientListResponse:
    del principal
    response = await service.mcp_usage_by_client()
    return McpClientListResponse(
        clients=[McpClientUsageResponse.from_domain(c) for c in response.data["clients"]]
    )


@router.get("/tools", response_model=McpToolListResponse)
async def mcp_tools(
    service: McpTelemetryServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> McpToolListResponse:
    del principal
    response = await service.mcp_usage_by_tool()
    return McpToolListResponse(
        tools=[McpToolUsageResponse.from_domain(t) for t in response.data["tools"]]
    )


@router.get("/queries", response_model=McpQueryListResponse)
async def mcp_queries(
    service: McpTelemetryServiceDependency,
    # McpUsageEntry has no tenant dimension either -- see mcp_clients above.
    principal: Annotated[Principal, Depends(require_operator_role("admin"))],
    range: RangeQuery = "24h",  # noqa: A002 - matches the public query parameter name
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> McpQueryListResponse:
    del principal
    response = await service.mcp_queries(since=range_start(range), limit=limit)
    return McpQueryListResponse(
        range=range,
        entries=[McpQueryEntryResponse.from_domain(e) for e in response.data["entries"]],
    )


@router.get("/config", response_model=McpConfigResponse, responses=CONFIG_ERROR_RESPONSES)
async def mcp_config(
    service: McpTelemetryServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> McpConfigResponse:
    del principal
    response = await service.mcp_config()
    if not response.ok:
        if response.data.get("error_type") == "HarborCapabilityError":
            raise HarborCapabilityError("MCP configuration has not been published yet")
        raise HarborConnectionError("MCP telemetry service is unavailable")
    return McpConfigResponse.from_domain(response.data["config"])
