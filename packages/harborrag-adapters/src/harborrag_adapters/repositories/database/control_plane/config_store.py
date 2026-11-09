"""SQL runtime-settings store: per-tenant overrides and an append-only revision log."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from harborrag_adapters.repositories.database.control_plane.schemas_settings import (
    SettingOverrideRow,
    SettingRevisionRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.config_store import (
    RevisionSource,
    SettingChange,
    SettingsRevision,
    SettingsSnapshot,
)
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.ports.config_store import SETTINGS_CHANGED_CHANNEL


def notify_statement(tenant_id: str) -> Select[tuple[str]]:
    """Return the statement that tells listening processes ``tenant_id`` changed."""
    return sa.select(sa.func.pg_notify(SETTINGS_CHANGED_CHANNEL, tenant_id))


@dataclass(frozen=True, slots=True)
class _Stamp:
    """Revision number, time and actor written onto every touched override row."""

    revision: int
    at: datetime
    by: str


def _revision_view(row: SettingRevisionRow) -> SettingsRevision:
    return SettingsRevision(
        tenant_id=row.tenant_id,
        revision=row.revision,
        source=row.source,  # type: ignore[arg-type]
        created_by=row.created_by,
        created_at=row.created_at,
        changes=tuple(
            SettingChange(
                level=entry["level"],
                key=entry["key"],
                from_value=entry["from"],
                to_value=entry["to"],
                applies=entry["applies"],
            )
            for entry in row.changes_json
        ),
    )


@dataclass(slots=True)
class SqlSettingsStore:
    """Persist settings overrides and revisions through async SQLAlchemy."""

    sessions: SessionFactory

    async def load(self, tenant_id: str) -> SettingsSnapshot:
        """Return the tenant's current overrides; an empty log is revision 0.

        On Postgres the revision and the overrides come from one REPEATABLE READ
        snapshot, so a concurrent write cannot pair revision N with N+1 values.
        """
        async with self.sessions() as session:
            if session.bind.dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            revision = await self._current_revision(session, tenant_id)
            rows = (
                await session.scalars(
                    sa.select(SettingOverrideRow).where(SettingOverrideRow.tenant_id == tenant_id)
                )
            ).all()
        overrides: dict[str, dict[str, object]] = {}
        for row in rows:
            overrides.setdefault(row.level, {})[row.setting_key] = row.value_json
        return SettingsSnapshot(tenant_id=tenant_id, revision=revision, overrides=overrides)

    async def latest_revision(self, tenant_id: str) -> int:
        """Return the tenant's latest revision number; 0 for an empty log."""
        async with self.sessions() as session:
            return await self._current_revision(session, tenant_id)

    async def revision(self, tenant_id: str, revision: int) -> SettingsRevision | None:
        """Return one logged revision, or ``None`` when it does not exist."""
        async with self.sessions() as session:
            row = await session.get(SettingRevisionRow, (tenant_id, revision))
        return None if row is None else _revision_view(row)

    async def write(
        self,
        tenant_id: str,
        changes: Sequence[SettingChange],
        *,
        expected_revision: int,
        source: RevisionSource,
        created_by: str,
    ) -> int:
        """Apply ``changes`` as the next revision and notify other processes.

        Args:
            tenant_id: Tenant to write.
            changes: Planned changes; must not be empty.
            expected_revision: Revision the caller planned against.
            source: Origin of the change.
            created_by: Actor recorded in the log.

        Returns:
            The new revision number.

        Raises:
            ValueError: ``changes`` is empty.
            HarborPreconditionFailedError: ``expected_revision`` is stale or a concurrent
                write took the next revision.
        """
        if not changes:
            raise ValueError("changes must not be empty")
        new_revision = expected_revision + 1
        now = datetime.now(UTC)
        stamp = _Stamp(new_revision, now, created_by)
        async with self.sessions.begin() as session:
            if await self._current_revision(session, tenant_id) != expected_revision:
                raise HarborPreconditionFailedError("settings revision does not match")
            session.add(
                SettingRevisionRow(
                    tenant_id=tenant_id,
                    revision=new_revision,
                    source=source,
                    created_by=created_by,
                    created_at=now,
                    changes_json=[
                        {
                            "level": change.level,
                            "key": change.key,
                            "from": change.from_value,
                            "to": change.to_value,
                            "applies": change.applies,
                        }
                        for change in changes
                    ],
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                raise HarborPreconditionFailedError("settings revision was taken") from exc
            for change in changes:
                await self._apply(session, tenant_id, change, stamp)
            if session.bind.dialect.name == "postgresql":
                await session.execute(notify_statement(tenant_id))
        return new_revision

    async def ping(self) -> None:
        """Raise when the store is unreachable."""
        async with self.sessions() as session:
            await session.execute(sa.select(1))

    @staticmethod
    async def _current_revision(session: AsyncSession, tenant_id: str) -> int:
        latest = await session.scalar(
            sa.select(sa.func.max(SettingRevisionRow.revision)).where(
                SettingRevisionRow.tenant_id == tenant_id
            )
        )
        return latest or 0

    @staticmethod
    async def _apply(
        session: AsyncSession,
        tenant_id: str,
        change: SettingChange,
        stamp: _Stamp,
    ) -> None:
        row = await session.get(SettingOverrideRow, (tenant_id, change.level, change.key))
        if change.to_value is None:
            if row is not None:
                await session.delete(row)
            return
        if row is None:
            session.add(
                SettingOverrideRow(
                    tenant_id=tenant_id,
                    level=change.level,
                    setting_key=change.key,
                    value_json=change.to_value,
                    revision=stamp.revision,
                    updated_at=stamp.at,
                    updated_by=stamp.by,
                )
            )
            return
        row.value_json = change.to_value
        row.revision = stamp.revision
        row.updated_at = stamp.at
        row.updated_by = stamp.by


__all__ = ["SqlSettingsStore", "notify_statement"]
