"""SQL implementation of the TenantDirectory port."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.database.ingestion_control.schema import INGESTION_TASKS

from .session import SessionFactory


@dataclass(slots=True)
class SqlTenantDirectory:
    """A tenant is known once ``ingestion_tasks`` holds a task for it, whatever its status."""

    sessions: SessionFactory

    async def exists(self, tenant_id: str) -> bool:
        async with self.sessions() as session:
            # EXISTS stops at the first row; ix_ingestion_tasks_tenant_submitted covers it.
            return bool(
                await session.scalar(
                    sa.select(sa.exists().where(INGESTION_TASKS.c.tenant_id == tenant_id))
                )
            )


__all__ = ["SqlTenantDirectory"]
