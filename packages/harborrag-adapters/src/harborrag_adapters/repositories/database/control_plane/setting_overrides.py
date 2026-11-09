"""SqlSettingOverrideRepository: per-scope runtime config overrides."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import SettingOverrideRow
from harborrag_core.contracts.errors import HarborPreconditionFailedError
from harborrag_core.domain.setting_override import SettingOverride
from harborrag_core.domain.setting_registry import ScopeLevel

from .mapping import utc_now
from .session import SessionFactory


def _row_to_domain(row: SettingOverrideRow) -> SettingOverride:
    # The DB's ck_setting_overrides_scope_level check constraint guarantees this,
    # but SQLAlchemy's column type is plain str.
    return SettingOverride(
        tenant_id=row.tenant_id,
        scope_level=cast(ScopeLevel, row.scope_level),
        scope_id=row.scope_id,
        key=row.key,
        value=row.value,
        version=row.version,
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass(slots=True)
class SqlSettingOverrideRepository:
    """SettingOverrideRepositoryPort over the setting_overrides table."""

    sessions: SessionFactory

    async def get_at(
        self, tenant_id: str, scope_level: ScopeLevel, scope_id: str
    ) -> dict[str, object]:
        """Raw overrides stored at exactly this scope (no merging with other levels)."""
        statement = sa.select(SettingOverrideRow).where(
            SettingOverrideRow.tenant_id == tenant_id,
            SettingOverrideRow.scope_level == scope_level,
            SettingOverrideRow.scope_id == scope_id,
        )
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return {row.key: row.value for row in rows}

    async def list_for_tenant(self, tenant_id: str) -> list[SettingOverride]:
        """Every override in ``tenant_id``, across all scopes, for resolution and export."""
        statement = sa.select(SettingOverrideRow).where(SettingOverrideRow.tenant_id == tenant_id)
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [_row_to_domain(row) for row in rows]

    async def put(
        self, override: SettingOverride, *, expected_version: int | None
    ) -> SettingOverride:
        """Upsert one override; advances `version`.

        Raises:
            HarborPreconditionFailedError: ``expected_version`` is set and does not
                match the stored version (or the row does not exist yet and
                ``expected_version`` is not ``None``).
        """
        async with self.sessions.begin() as session:
            row = await session.get(
                SettingOverrideRow,
                (override.tenant_id, override.scope_level, override.scope_id, override.key),
                with_for_update=True,
            )
            if row is None:
                if expected_version is not None:
                    raise HarborPreconditionFailedError("setting override does not exist yet")
                session.add(
                    SettingOverrideRow(
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
                return replace(override, version=1)
            if expected_version is not None and expected_version != row.version:
                raise HarborPreconditionFailedError("setting override version does not match")
            row.version += 1
            row.value = override.value
            row.updated_by = override.updated_by
            row.updated_at = utc_now()
            return _row_to_domain(row)

    async def delete(
        self, tenant_id: str, scope_level: ScopeLevel, scope_id: str, key: str
    ) -> None:
        """Remove one override; a missing row is not an error."""
        statement = sa.delete(SettingOverrideRow).where(
            SettingOverrideRow.tenant_id == tenant_id,
            SettingOverrideRow.scope_level == scope_level,
            SettingOverrideRow.scope_id == scope_id,
            SettingOverrideRow.key == key,
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)

    async def count_all(self) -> int:
        """Total override rows across every tenant, for the empty-database seed guard."""
        async with self.sessions() as session:
            result = await session.scalar(
                sa.select(sa.func.count()).select_from(SettingOverrideRow)
            )
            return int(result or 0)
