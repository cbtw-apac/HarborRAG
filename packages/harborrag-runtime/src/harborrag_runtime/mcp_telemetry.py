"""The bridge harborrag-mcp-server uses to reach the control-plane DB (ML4-P3).

The MCP server is not allowed to depend on ``harborrag_adapters``/the DB
directly (see ``scripts/check_dependency_direction.py``); this module is the
"separate internal component" the ML4-P3 ticket describes, wrapping the
adapter/DB access harborrag-mcp-server needs behind a narrow interface it is
allowed to import (``harborrag_runtime`` only). It mirrors
``harborrag_runtime.memory.build_database_conversation_memory``: migrate,
connect, hand back a small facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harborrag_core.domain.mcp_usage import McpConfigSnapshot
    from harborrag_core.ports.control_plane import McpConfigSnapshotPort, McpQueryLogRepositoryPort
    from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(slots=True)
class McpTelemetryBridge:
    """The one control-plane-DB surface harborrag-mcp-server is allowed to call."""

    query_log: McpQueryLogRepositoryPort
    config_snapshot: McpConfigSnapshotPort
    engine: AsyncEngine

    async def record_usage(
        self,
        *,
        tool: str,
        client: str,
        latency_ms: int,
        created_at: datetime,
    ) -> None:
        """Persist one completed tool invocation with real end-to-end timing."""
        from harborrag_core.domain.mcp_usage import McpUsageEntry

        await self.query_log.record(
            McpUsageEntry(tool=tool, client=client, latency_ms=latency_ms, created_at=created_at)
        )

    async def publish_config(self, snapshot: McpConfigSnapshot) -> None:
        """Publish the MCP server's current effective configuration for app-side reads."""
        await self.config_snapshot.put(snapshot)

    async def status(self) -> bool:
        """True if the control-plane DB answers a query right now."""
        return await self.query_log.ping()

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_mcp_telemetry_bridge(settings: RuntimeSettings | None = None) -> McpTelemetryBridge:
    """Migrate and open the configured control database for a standalone MCP server process."""

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_config_snapshot import (
        SqlMcpConfigSnapshotRepository,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_query_log import (
        SqlMcpQueryLogRepository,
    )
    from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
    from harborrag_runtime.config.settings import RuntimeSettings

    selected = settings or RuntimeSettings()
    dsn = selected.control_db_url.get_secret_value()
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    return McpTelemetryBridge(
        query_log=SqlMcpQueryLogRepository(sessions),
        config_snapshot=SqlMcpConfigSnapshotRepository(sessions),
        engine=engine,
    )


__all__ = ["McpTelemetryBridge", "build_mcp_telemetry_bridge"]
