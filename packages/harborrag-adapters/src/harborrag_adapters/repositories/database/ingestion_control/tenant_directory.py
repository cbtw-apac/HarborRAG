"""SQL implementation of the TenantDirectory port over ``source_scopes``.

The listing crosses tenants. Hand it only to an operator surface that has already
checked the caller may name any tenant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.schemas.ids import TenantId

from .schema import SOURCE_SCOPES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_DEFAULT_LIMIT = 500

logger = logging.getLogger("harborrag.adapters.ingestion_control.tenant_directory")


class SqlTenantDirectory:
    """TenantDirectory backed by the distinct tenants of the ``source_scopes`` table.

    Args:
        engine: Engine of the database that holds the ingestion tables.
        limit: The most tenants one listing returns.
    """

    def __init__(self, engine: AsyncEngine, *, limit: int = _DEFAULT_LIMIT) -> None:
        self._engine = engine
        self._limit = limit

    async def tenant_ids(self) -> list[TenantId]:
        """List the tenants that own a source scope, sorted and without repeats.

        A listing cut short by ``limit`` logs a warning with the limit.

        Returns:
            The tenant IDs, at most ``limit`` of them.

        Raises:
            HarborUnavailableError: The database cannot be reached or read.
        """
        statement = (
            select(SOURCE_SCOPES.c.tenant_id)
            .distinct()
            .order_by(SOURCE_SCOPES.c.tenant_id)
            .limit(self._limit + 1)
        )
        try:
            async with self._engine.connect() as connection:
                tenants = list(await connection.scalars(statement))
        except (SQLAlchemyError, OSError) as exc:
            raise HarborUnavailableError("tenant directory is unavailable") from exc
        if len(tenants) > self._limit:
            logger.warning("Tenant directory listing truncated limit=%d", self._limit)
        return [TenantId(tenant) for tenant in tenants[: self._limit]]
