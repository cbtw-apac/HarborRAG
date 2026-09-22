"""MCP telemetry read models: usage rows written by harborrag-mcp-server.

``client`` on every record is the caller's authenticated account id
(``principal_id`` at the MCP transport boundary), used as a best-effort
stand-in for true MCP client identity -- the protocol does not hand the
server a stable client name, so this is the closest real signal available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class McpUsageEntry:
    """One recorded MCP tool invocation, with real end-to-end timing."""

    tool: str
    client: str
    latency_ms: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class McpClientUsage:
    """Usage rolled up by caller: how much, how recently."""

    client: str
    query_count: int
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class McpToolUsage:
    """Usage rolled up by tool: how often, how fast."""

    tool: str
    call_count: int
    avg_latency_ms: float


@dataclass(frozen=True, slots=True)
class McpConfigSnapshot:
    """The MCP server's effective configuration, published by harborrag-mcp-server.

    Mirrors ``McpConfigurationStore.describe()`` on the write side; the
    control-plane DB is the only channel harborrag-app is allowed to read
    it through, since the layering rules forbid an app -> mcp-server import.
    """

    policy: dict[str, int | bool]
    disabled_tools: tuple[str, ...]
    enabled_tool_count: int
    total_tool_count: int
    revision: str
    restart_required: bool
    updated_at: datetime
