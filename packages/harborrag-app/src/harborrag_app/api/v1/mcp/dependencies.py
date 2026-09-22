"""Narrow application-service dependency for MCP telemetry routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.schemas import AppResponse


class McpTelemetryService(Protocol):
    async def mcp_status(self) -> AppResponse: ...

    async def mcp_usage_by_client(self) -> AppResponse: ...

    async def mcp_usage_by_tool(self) -> AppResponse: ...

    async def mcp_queries(self, *, since: datetime, limit: int) -> AppResponse: ...

    async def mcp_config(self) -> AppResponse: ...


def mcp_telemetry_service(request: Request) -> McpTelemetryService:
    return cast(McpTelemetryService, request.app.state.app_service)


McpTelemetryServiceDependency = Annotated[McpTelemetryService, Depends(mcp_telemetry_service)]
