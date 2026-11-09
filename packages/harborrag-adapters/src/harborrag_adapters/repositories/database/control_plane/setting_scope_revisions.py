"""SqlSettingScopeRevisionRepository: one monotonic revision per setting scope."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.repositories.database.control_plane.schemas import SettingScopeRevisionRow
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_registry import ScopeLevel

from .mapping import utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlSettingScopeRevisionRepository:
    """SettingScopeRevisionRepositoryPort over the setting_scope_revisions table."""

    sessions: SessionFactory

    async def get(self, tenant_id: str, scope_level: ScopeLevel, scope_id: str) -> int:
        """Current revision, or 0 when the scope has never been written."""
        async with self.sessions() as session:
            row = await session.get(SettingScopeRevisionRow, (tenant_id, scope_level, scope_id))
            return row.revision if row is not None else 0

    async def bump_if_match(
        self,
        tenant_id: str,
        scope_level: ScopeLevel,
        scope_id: str,
        *,
        expected_revision: int,
    ) -> int:
        """Advance the scope's revision by one and return it.

        Raises:
            HarborPreconditionFailedError: ``expected_revision`` does not match
                the stored revision (0 when the scope has never been written).
        """
        async with self.sessions.begin() as session:
            row = await session.get(
                SettingScopeRevisionRow,
                (tenant_id, scope_level, scope_id),
                with_for_update=True,
            )
            current = row.revision if row is not None else 0
            if expected_revision != current:
                raise HarborPreconditionFailedError("setting scope revision does not match")
            if row is None:
                session.add(
                    SettingScopeRevisionRow(
                        tenant_id=tenant_id,
                        scope_level=scope_level,
                        scope_id=scope_id,
                        revision=1,
                        updated_at=utc_now(),
                    )
                )
                return 1
            row.revision += 1
            row.updated_at = utc_now()
            return row.revision
