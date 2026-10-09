"""User-erasure journal ORM row (migration 0040).

Own module for the file-length gate; imported from schemas.py so it registers
on the shared ``Base`` metadata for Alembic and the metadata-drift test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base, JSONVariant


class UserErasureRow(Base):
    """One right-to-erasure run: identifiers, status, and per-store counts only."""

    __tablename__ = "user_erasures"
    __table_args__ = (
        sa.Index("ix_user_erasures_subject", "tenant_id", "subject_user_id", "started_at"),
    )

    erasure_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    subject_user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    counts_json: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    failed_steps_json: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
