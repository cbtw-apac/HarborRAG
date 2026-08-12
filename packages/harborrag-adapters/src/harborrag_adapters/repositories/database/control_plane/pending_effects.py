"""SqlPendingEffectRepository: PendingEffectRepositoryPort over the retry queue table."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_core.domain.pending_effect import PendingControlPlaneEffect

from .schemas import PendingControlPlaneEffectRow
from .session import SessionFactory


@dataclass(slots=True)
class SqlPendingEffectRepository:
    """PendingEffectRepositoryPort over pending_control_plane_effects."""

    sessions: SessionFactory

    async def enqueue(self, effect: PendingControlPlaneEffect) -> None:
        """Durably record a failed side effect for later retry."""
        async with self.sessions.begin() as session:
            session.add(
                PendingControlPlaneEffectRow(
                    id=effect.id,
                    kind=effect.kind,
                    payload_json=effect.payload,
                    created_at=effect.created_at,
                )
            )

    async def list_pending(self, *, limit: int = 100) -> list[PendingControlPlaneEffect]:
        """Oldest-first pending effects, for the recovery drain."""
        async with self.sessions() as session:
            rows = await session.scalars(
                sa.select(PendingControlPlaneEffectRow)
                .order_by(
                    PendingControlPlaneEffectRow.created_at,
                    PendingControlPlaneEffectRow.id,
                )
                .limit(limit)
            )
            return [
                PendingControlPlaneEffect(
                    id=row.id,
                    kind=row.kind,
                    payload=row.payload_json,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def complete(self, effect_id: str) -> None:
        """Remove an effect once its retry has succeeded; a no-op if already gone."""
        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(PendingControlPlaneEffectRow).where(
                    PendingControlPlaneEffectRow.id == effect_id
                )
            )
