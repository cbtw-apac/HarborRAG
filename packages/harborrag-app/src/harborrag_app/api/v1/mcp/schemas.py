"""Strict public schemas for MCP telemetry endpoints (ML4-P3)."""

from __future__ import annotations

from datetime import datetime

from harborrag_app.api.schemas import ApiModel
from harborrag_core.domain.mcp_usage import (
    McpClientUsage,
    McpConfigSnapshot,
    McpToolUsage,
    McpUsageEntry,
)


class McpStatusResponse(ApiModel):
    """Whether the control-plane DB backing MCP telemetry is reachable and healthy."""

    reachable: bool
    healthy: bool


class McpClientUsageResponse(ApiModel):
    client: str
    query_count: int
    last_seen_at: datetime

    @classmethod
    def from_domain(cls, usage: McpClientUsage) -> McpClientUsageResponse:
        return cls(
            client=usage.client,
            query_count=usage.query_count,
            last_seen_at=usage.last_seen_at,
        )


class McpClientListResponse(ApiModel):
    clients: list[McpClientUsageResponse]


class McpToolUsageResponse(ApiModel):
    tool: str
    call_count: int
    avg_latency_ms: float

    @classmethod
    def from_domain(cls, usage: McpToolUsage) -> McpToolUsageResponse:
        return cls(
            tool=usage.tool,
            call_count=usage.call_count,
            avg_latency_ms=usage.avg_latency_ms,
        )


class McpToolListResponse(ApiModel):
    tools: list[McpToolUsageResponse]


class McpQueryEntryResponse(ApiModel):
    tool: str
    client: str
    latency_ms: int
    created_at: datetime

    @classmethod
    def from_domain(cls, entry: McpUsageEntry) -> McpQueryEntryResponse:
        return cls(
            tool=entry.tool,
            client=entry.client,
            latency_ms=entry.latency_ms,
            created_at=entry.created_at,
        )


class McpQueryListResponse(ApiModel):
    range: str
    entries: list[McpQueryEntryResponse]


class McpConfigResponse(ApiModel):
    """The MCP server's last-published effective configuration; no placeholder fields."""

    policy: dict[str, int | bool]
    disabled_tools: list[str]
    enabled_tool_count: int
    total_tool_count: int
    revision: str
    restart_required: bool
    updated_at: datetime

    @classmethod
    def from_domain(cls, snapshot: McpConfigSnapshot) -> McpConfigResponse:
        return cls(
            policy=dict(snapshot.policy),
            disabled_tools=list(snapshot.disabled_tools),
            enabled_tool_count=snapshot.enabled_tool_count,
            total_tool_count=snapshot.total_tool_count,
            revision=snapshot.revision,
            restart_required=snapshot.restart_required,
            updated_at=snapshot.updated_at,
        )
