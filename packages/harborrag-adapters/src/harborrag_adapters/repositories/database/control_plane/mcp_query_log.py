"""SqlMcpQueryLogRepository: McpQueryLogRepositoryPort over mcp_query_log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from harborrag_core.domain.mcp_usage import McpClientUsage, McpToolUsage, McpUsageEntry

from .schemas import McpQueryLogRow
from .session import SessionFactory


@dataclass(slots=True)
class SqlMcpQueryLogRepository:
    """McpQueryLogRepositoryPort over the mcp_query_log table (ML4-P3)."""

    sessions: SessionFactory

    async def record(self, entry: McpUsageEntry) -> None:
        """Durably record one completed MCP tool invocation."""
        async with self.sessions.begin() as session:
            session.add(
                McpQueryLogRow(
                    tool=entry.tool,
                    client=entry.client,
                    latency_ms=entry.latency_ms,
                    created_at=entry.created_at,
                )
            )

    async def usage_by_client(self) -> list[McpClientUsage]:
        """Every distinct client, with its total query count and last-seen time."""
        statement = (
            sa.select(
                McpQueryLogRow.client,
                sa.func.count(McpQueryLogRow.id),
                sa.func.max(McpQueryLogRow.created_at),
            )
            .group_by(McpQueryLogRow.client)
            .order_by(sa.func.max(McpQueryLogRow.created_at).desc())
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return [
            McpClientUsage(client=client, query_count=count, last_seen_at=last_seen)
            for client, count, last_seen in rows
        ]

    async def usage_by_tool(self) -> list[McpToolUsage]:
        """Every distinct tool, with its call count and average latency."""
        statement = (
            sa.select(
                McpQueryLogRow.tool,
                sa.func.count(McpQueryLogRow.id),
                sa.func.avg(McpQueryLogRow.latency_ms),
            )
            .group_by(McpQueryLogRow.tool)
            .order_by(sa.func.count(McpQueryLogRow.id).desc())
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return [
            McpToolUsage(tool=tool, call_count=count, avg_latency_ms=float(avg_latency))
            for tool, count, avg_latency in rows
        ]

    async def list_since(self, *, since: datetime, limit: int) -> list[McpUsageEntry]:
        """Entries at or after ``since``, newest first, capped at ``limit``."""
        statement = (
            sa.select(McpQueryLogRow)
            .where(McpQueryLogRow.created_at >= since)
            .order_by(McpQueryLogRow.created_at.desc(), McpQueryLogRow.id.desc())
            .limit(limit)
        )
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        return [
            McpUsageEntry(
                tool=row.tool,
                client=row.client,
                latency_ms=row.latency_ms,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def ping(self) -> bool:
        """Best-effort reachability check for ``/mcp/status``: True if the store answers."""
        async with self.sessions() as session:
            await session.execute(sa.select(sa.literal(1)))
        return True
