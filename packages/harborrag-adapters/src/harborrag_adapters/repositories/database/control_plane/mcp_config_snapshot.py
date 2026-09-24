"""SqlMcpConfigSnapshotRepository: McpConfigSnapshotPort over mcp_config_snapshot."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_core.domain.mcp_usage import McpConfigSnapshot

from .schemas import McpConfigSnapshotRow
from .session import SessionFactory

_FIXED_ROW_ID = 1


@dataclass(slots=True)
class SqlMcpConfigSnapshotRepository:
    """McpConfigSnapshotPort over the single-row mcp_config_snapshot table (ML4-P3)."""

    sessions: SessionFactory

    async def get(self) -> McpConfigSnapshot | None:
        """The most recently published snapshot, or None if never published."""
        async with self.sessions() as session:
            row = await session.get(McpConfigSnapshotRow, _FIXED_ROW_ID)
        if row is None:
            return None
        return McpConfigSnapshot(
            policy=dict(row.policy_json),
            disabled_tools=tuple(row.disabled_tools_json),
            enabled_tool_count=row.enabled_tool_count,
            total_tool_count=row.total_tool_count,
            revision=row.revision,
            restart_required=row.restart_required,
            updated_at=row.updated_at,
        )

    async def put(self, snapshot: McpConfigSnapshot) -> McpConfigSnapshot:
        """Replace the published snapshot and return it."""
        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(McpConfigSnapshotRow).where(McpConfigSnapshotRow.id == _FIXED_ROW_ID)
            )
            session.add(
                McpConfigSnapshotRow(
                    id=_FIXED_ROW_ID,
                    policy_json=dict(snapshot.policy),
                    disabled_tools_json=list(snapshot.disabled_tools),
                    enabled_tool_count=snapshot.enabled_tool_count,
                    total_tool_count=snapshot.total_tool_count,
                    revision=snapshot.revision,
                    restart_required=snapshot.restart_required,
                    updated_at=snapshot.updated_at,
                )
            )
        return snapshot
