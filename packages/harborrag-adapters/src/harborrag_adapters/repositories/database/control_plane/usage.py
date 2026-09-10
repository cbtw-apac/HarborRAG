"""SQL model-usage accounting adapter for the control-plane database.

``record`` is deliberately best-effort: a failed accounting write is logged
with identifiers only and swallowed, because the provider call it describes
has already been paid for and the caller is owed its answer. ``totals`` is
the read side and does raise -- a wrong number is worse than no number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from harborrag_adapters.repositories.database.control_plane.schemas_usage import ModelUsageRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.usage import ModelUsageRecord, ModelUsageTotals

logger = logging.getLogger("harborrag.adapters.control_plane.usage")


def _row(usage: ModelUsageRecord) -> ModelUsageRow:
    return ModelUsageRow(
        id=usage.id,
        tenant_id=usage.tenant_id,
        user_id=usage.user_id,
        principal_id=usage.principal_id,
        session_id=usage.session_id,
        run_id=usage.run_id,
        surface=usage.surface,
        logical_model=usage.logical_model,
        provider=usage.provider,
        provider_model=usage.provider_model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
        finish_reason=usage.finish_reason,
        created_at=usage.created_at,
    )


_TOTALS = (
    sa.func.count().label("requests"),
    sa.func.coalesce(sa.func.sum(ModelUsageRow.prompt_tokens), 0).label("prompt_tokens"),
    sa.func.coalesce(sa.func.sum(ModelUsageRow.completion_tokens), 0).label("completion_tokens"),
    sa.func.coalesce(sa.func.sum(ModelUsageRow.total_tokens), 0).label("total_tokens"),
    sa.func.coalesce(sa.func.sum(ModelUsageRow.estimated_cost_usd), 0.0).label("cost"),
)


@dataclass(slots=True)
class SqlModelUsageRepository:
    """Persist and aggregate model usage through async SQLAlchemy."""

    sessions: SessionFactory

    async def record(self, usage: ModelUsageRecord) -> None:
        try:
            async with self.sessions.begin() as session:
                session.add(_row(usage))
        except Exception as exc:  # noqa: BLE001 - accounting must not fail an answer
            logger.error(
                "Model usage record failed tenant_id=%s user_id=%s session_id=%s "
                "surface=%s usage_id=%s error_type=%s",
                usage.tenant_id,
                usage.user_id,
                usage.session_id,
                usage.surface,
                usage.id,
                type(exc).__name__,
            )

    async def totals(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        since: datetime | None = None,
    ) -> ModelUsageTotals:
        conditions: list[ColumnElement[bool]] = [ModelUsageRow.tenant_id == tenant_id]
        if user_id is not None:
            conditions.append(ModelUsageRow.user_id == user_id)
        if since is not None:
            conditions.append(ModelUsageRow.created_at >= since)
        statement = sa.select(*_TOTALS).where(*conditions)
        async with self.sessions() as session:
            row = (await session.execute(statement)).one()
        return ModelUsageTotals(
            requests=int(row.requests),
            prompt_tokens=int(row.prompt_tokens),
            completion_tokens=int(row.completion_tokens),
            total_tokens=int(row.total_tokens),
            estimated_cost_usd=float(row.cost),
        )


__all__ = ["SqlModelUsageRepository"]
