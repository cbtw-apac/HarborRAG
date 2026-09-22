"""Canonical MCP telemetry payloads shared by the app test doubles."""

from __future__ import annotations

from datetime import UTC, datetime

from harborrag_core.domain.mcp_usage import (
    McpClientUsage,
    McpConfigSnapshot,
    McpToolUsage,
    McpUsageEntry,
)


def mcp_usage_entry(
    *,
    tool: str = "retrieval_search",
    client: str = "dev",
    latency_ms: int = 42,
    created_at: datetime = datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
) -> McpUsageEntry:
    return McpUsageEntry(tool=tool, client=client, latency_ms=latency_ms, created_at=created_at)


def mcp_client_usage() -> McpClientUsage:
    return McpClientUsage(
        client="dev",
        query_count=3,
        last_seen_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )


def mcp_tool_usage() -> McpToolUsage:
    return McpToolUsage(tool="retrieval_search", call_count=3, avg_latency_ms=42.5)


def mcp_config_snapshot() -> McpConfigSnapshot:
    return McpConfigSnapshot(
        policy={"max_results": 20, "max_argument_bytes": 65536, "allow_ingestion": False},
        disabled_tools=("ingestion_run",),
        enabled_tool_count=4,
        total_tool_count=5,
        revision="rev-1",
        restart_required=False,
        updated_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )


__all__ = [
    "mcp_client_usage",
    "mcp_config_snapshot",
    "mcp_tool_usage",
    "mcp_usage_entry",
]
