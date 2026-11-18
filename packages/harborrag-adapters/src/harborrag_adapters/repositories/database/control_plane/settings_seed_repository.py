"""SqlSettingsSeedRepository: write the one-time settings seed as one transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    RuntimeSettingRevisionRow,
    RuntimeSettingRow,
    WorkspaceSettingsRow,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.setting_override import SettingOverride

from .mapping import utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlSettingsSeedRepository:
    """SettingsSeedRepositoryPort over runtime_settings(_revisions) and workspace_settings.

    Everything here runs in one transaction: a failure on any write rolls
    every one of them back, so the next boot's `setting_overrides` empty
    check still holds and the seed retries cleanly instead of getting stuck
    half-applied.
    """

    sessions: SessionFactory

    async def seed(
        self,
        *,
        tenant_id: str,
        overrides: Sequence[SettingOverride],
        workspace_tenant_id: str,
        marker_key: str,
        marker_value: dict[str, Any],
    ) -> None:
        now = utc_now()
        async with self.sessions.begin() as session:
            existing = await session.scalar(
                sa.select(sa.func.count()).select_from(RuntimeSettingRow)
            )
            if existing:
                raise HarborConflictError(
                    "setting_overrides already holds rows; refusing to seed over them"
                )
            revision_row = await session.get(
                RuntimeSettingRevisionRow,
                (tenant_id, "tenant", tenant_id),
                with_for_update=True,
            )
            if revision_row is not None and revision_row.revision != 0:
                raise HarborConflictError(
                    f"tenant {tenant_id!r} scope already has a revision; refusing to seed"
                )
            if overrides:
                if revision_row is None:
                    session.add(
                        RuntimeSettingRevisionRow(
                            tenant_id=tenant_id,
                            scope_level="tenant",
                            scope_id=tenant_id,
                            revision=1,
                            source="seed",
                            actor=None,
                            updated_at=now,
                        )
                    )
                else:
                    revision_row.revision = 1
                    revision_row.source = "seed"
                    revision_row.actor = None
                    revision_row.updated_at = now
            for override in overrides:
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
            workspace_row = await session.get(WorkspaceSettingsRow, 1, with_for_update=True)
            if workspace_row is None:
                session.add(
                    WorkspaceSettingsRow(
                        id=1,
                        tenant_id=workspace_tenant_id,
                        data={marker_key: marker_value},
                        updated_at=now,
                    )
                )
            else:
                workspace_row.data = {**workspace_row.data, marker_key: marker_value}
                workspace_row.updated_at = now
