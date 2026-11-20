"""SqlSettingScopeRevisionRepository: one monotonic revision per setting scope."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.database.control_plane.schemas import RuntimeSettingRevisionRow
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_registry import ScopeLevel
from harborrag_core.ports.control_plane import ScopeRevisionRecord

from .mapping import utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlSettingScopeRevisionRepository:
    """SettingScopeRevisionRepositoryPort over the runtime_settings_revisions table."""

    sessions: SessionFactory

    async def get(self, tenant_id: str, scope_level: ScopeLevel, scope_id: str) -> int:
        """Current revision, or 0 when the scope has never been written."""
        async with self.sessions() as session:
            row = await session.get(RuntimeSettingRevisionRow, (tenant_id, scope_level, scope_id))
            return row.revision if row is not None else 0

    async def get_record(
        self, tenant_id: str, scope_level: ScopeLevel, scope_id: str
    ) -> ScopeRevisionRecord | None:
        """Revision plus ``source``/``actor`` of the last bump, or ``None`` if never written."""
        async with self.sessions() as session:
            row = await session.get(RuntimeSettingRevisionRow, (tenant_id, scope_level, scope_id))
            if row is None:
                return None
            return {"revision": row.revision, "source": row.source, "actor": row.actor}

    async def bump_if_match(
        self,
        tenant_id: str,
        scope_level: ScopeLevel,
        scope_id: str,
        *,
        expected_revision: int,
        source: str,
        actor: str | None = None,
    ) -> int:
        """Advance the scope's revision by one and return it.

        Raises:
            HarborPreconditionFailedError: ``expected_revision`` does not match
                the stored revision (0 when the scope has never been written).
        """
        async with self.sessions.begin() as session:
            await self._ensure_row(session, tenant_id, scope_level, scope_id)
            row = await session.get(
                RuntimeSettingRevisionRow,
                (tenant_id, scope_level, scope_id),
                with_for_update=True,
            )
            assert row is not None  # noqa: S101 - _ensure_row guarantees the row exists
            if expected_revision != row.revision:
                raise HarborPreconditionFailedError("setting scope revision does not match")
            row.revision += 1
            row.source = source
            row.actor = actor
            row.updated_at = utc_now()
            return row.revision

    @staticmethod
    async def _ensure_row(
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
