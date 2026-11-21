"""Readable source scopes, read from the ingestion control database's permission snapshots.

The database connects on first use, so a process that never lists connections for a
reader opens no pool. The owner closes it once.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from harborrag_runtime.composition.resources import build_ingestion_control

if TYPE_CHECKING:
    from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
    from harborrag_core.security import AccessContext
    from harborrag_runtime.config.settings import RuntimeSettings


class ControlSourceAuthorizer:
    """``SourceScopeAuthorizer`` over the ingestion control database.

    Args:
        settings: Settings the control database is built from.
    """

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._control: IngestionControlPlaneDatabase | None = None
        self._lock = asyncio.Lock()

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        """Return every source scope ``access`` may read in ``tenant_id``.

        Raises:
            HarborConflictError: The reader may read more than ``limit`` source scopes.
        """
        control = await self._connected()
        return await control.topology.allowed_source_scope_ids(
            tenant_id, access=access, limit=limit
        )

    async def aclose(self) -> None:
        """Close the control database when it was opened."""
        control, self._control = self._control, None
        if control is not None:
            await control.close()

    async def _connected(self) -> IngestionControlPlaneDatabase:
        """Return the control database, connecting it on first use."""
        async with self._lock:
            if self._control is None:
                control = build_ingestion_control(self._settings)
                try:
                    await control.connect()
                except BaseException:
                    await control.close()
                    raise
                self._control = control
            return self._control
