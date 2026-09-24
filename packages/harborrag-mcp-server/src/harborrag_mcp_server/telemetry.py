"""Build the read-only config snapshot published to the control-plane DB (ML4-P3)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harborrag_core.domain.mcp_usage import McpConfigSnapshot
from harborrag_core.invariants import HarborInvariantError

if TYPE_CHECKING:
    from harborrag_mcp_server.configuration import McpConfigurationStore
    from harborrag_mcp_server.server.server import McpServer


def build_config_snapshot(
    registry: McpServer,
    configuration: McpConfigurationStore,
) -> McpConfigSnapshot:
    """Derive the current effective configuration the MCP server is actually running with."""
    if registry.tools is None:
        raise HarborInvariantError("registry.tools must not be None here")
    description = configuration.describe()
    all_tool_names = sorted(tool.spec.name for tool in registry.tools)
    enabled_tool_names = set(configuration.enabled_tool_names())
    disabled_tools = tuple(name for name in all_tool_names if name not in enabled_tool_names)
    return McpConfigSnapshot(
        policy=asdict(configuration.policy()),
        disabled_tools=disabled_tools,
        enabled_tool_count=len(enabled_tool_names),
        total_tool_count=len(all_tool_names),
        revision=str(description["revision"]),
        restart_required=bool(description["restart_required"]),
        updated_at=datetime.now(UTC),
    )


__all__ = ["build_config_snapshot"]
