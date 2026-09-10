"""Model-usage accounting ORM row.

Kept in its own module (file-length gate) and imported from schemas.py so it
registers on the shared ``Base`` metadata -- required for Alembic
autogenerate and the metadata-drift test to see the table.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base


class ModelUsageRow(Base):
    """One model call's token and cost footprint (migration 0025).

    ``tenant_id``/``user_id``/``created_at`` are indexed because every
    aggregation is "this tenant, optionally this human, optionally since
    then"; ``principal_id`` is recorded for audit only.
    """

    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False, index=True)
    principal_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    session_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # "chat" | "agent": the completion surface that issued the model call.
    surface: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    logical_model: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    estimated_cost_usd: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
