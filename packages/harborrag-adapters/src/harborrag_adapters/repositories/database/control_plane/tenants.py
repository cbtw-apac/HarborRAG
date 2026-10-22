"""SQL implementation of the TenantDirectory port."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

# Depends on the ingestion_control schema: tenants are derived from its task table.
from harborrag_adapters.repositories.database.ingestion_control.schema import INGESTION_TASKS
from harborrag_core.schemas.storage import StorageOperationContext

from .session import SessionFactory
from .store_errors import store_errors


@dataclass(slots=True)
class SqlTenantDirectory:
    """A tenant is known once ``ingestion_tasks`` holds a task for it, whatever its status.

    Store failures raise ``AuthStoreUnavailable``, like the MCP key repositories.
    Tenant discovery is global, so it needs a platform-scoped context.
    """

    sessions: SessionFactory

    async def exists(self, tenant_id: str, *, context: StorageOperationContext) -> bool:
        context.require_platform()
        with store_errors("Tenant directory is unavailable"):
            async with self.sessions() as session:
                # EXISTS stops at the first row; ix_ingestion_tasks_tenant_submitted covers it.
                return bool(
                    await session.scalar(
                        sa.select(sa.exists().where(INGESTION_TASKS.c.tenant_id == tenant_id))
                    )
                )


__all__ = ["SqlTenantDirectory"]
