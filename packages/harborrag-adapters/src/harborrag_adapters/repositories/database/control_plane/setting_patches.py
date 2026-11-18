"""SqlSettingPatchRepository: one PATCH /v1/admin/settings write as a single transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.database.control_plane.schemas import (
    RuntimeSettingRevisionRow,
    RuntimeSettingRow,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.domain.setting_registry import ScopeLevel

from .mapping import utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlSettingPatchRepository:
    """SettingsPatchRepositoryPort over the runtime_settings(_revisions) tables.

    Everything runs under one row lock on the scope's revision row, held for
    the whole write: the bump and every override put/delete commit together,
    so a concurrent PATCH on the same scope cannot interleave with this one.
    """

    sessions: SessionFactory

    async def apply(
        self,
        tenant_id: str,
        scope_level: ScopeLevel,
        scope_id: str,
        *,
        expected_revision: int,
        source: str,
        actor: str | None,
        puts: Sequence[SettingOverride],
        deletes: Sequence[str],
    ) -> int:
        """Bump the scope's revision and write every override change; return the new revision.

        Raises:
            HarborPreconditionFailedError: ``expected_revision`` does not match
                the stored revision (0 when the scope has never been written).
        """
        now = utc_now()
        async with self.sessions.begin() as session:
            await self._ensure_revision_row(session, tenant_id, scope_level, scope_id)
            revision_row = await session.get(
                RuntimeSettingRevisionRow,
                (tenant_id, scope_level, scope_id),
                with_for_update=True,
            )
            assert revision_row is not None  # noqa: S101 - _ensure_revision_row guarantees it
            if expected_revision != revision_row.revision:
                raise HarborPreconditionFailedError("setting scope revision does not match")
            new_revision = expected_revision + 1
            result = await session.execute(
                sa.update(RuntimeSettingRevisionRow)
                .where(
                    RuntimeSettingRevisionRow.tenant_id == tenant_id,
                    RuntimeSettingRevisionRow.scope_level == scope_level,
                    RuntimeSettingRevisionRow.scope_id == scope_id,
                    RuntimeSettingRevisionRow.revision == expected_revision,
                )
                .values(revision=new_revision, source=source, actor=actor, updated_at=now)
            )
            if result.rowcount != 1:
                raise HarborPreconditionFailedError("setting scope revision does not match")

            for key in deletes:
                await session.execute(
                    sa.delete(RuntimeSettingRow).where(
                        RuntimeSettingRow.tenant_id == tenant_id,
                        RuntimeSettingRow.scope_level == scope_level,
                        RuntimeSettingRow.scope_id == scope_id,
                        RuntimeSettingRow.key == key,
                    )
                )

            for override in puts:
                row = await session.get(
                    RuntimeSettingRow,
                    (override.tenant_id, override.scope_level, override.scope_id, override.key),
                    with_for_update=True,
                )
                if row is None:
                    session.add(
                        RuntimeSettingRow(
                            tenant_id=override.tenant_id,
                            scope_level=override.scope_level,
                            scope_id=override.scope_id,
                            key=override.key,
                            value=override.value,
                            version=1,
                            created_by=override.created_by,
                            updated_by=override.updated_by,
                            created_at=override.created_at,
                            updated_at=override.updated_at,
                        )
                    )
                else:
                    row.version += 1
                    row.value = override.value
                    row.updated_by = override.updated_by
                    row.updated_at = now

            return new_revision

    @staticmethod
    async def _ensure_revision_row(
        session: AsyncSession, tenant_id: str, scope_level: ScopeLevel, scope_id: str
    ) -> None:
        """Insert a revision-0 row for this scope unless one already exists.

        Conflict-safe: two concurrent first writes to the same scope can both
        reach this with no row present (a Postgres row lock cannot hold on a
        row that does not exist yet), so this must tolerate the other side
        winning the insert rather than racing a plain SELECT-then-INSERT.
        """
        factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
        await session.execute(
            factory(RuntimeSettingRevisionRow)
            .values(
                tenant_id=tenant_id,
                scope_level=scope_level,
                scope_id=scope_id,
                revision=0,
                source="init",
                actor=None,
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "scope_level", "scope_id"])
        )
