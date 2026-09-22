"""Idempotent completion request claims and replayable terminal responses."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base


class CompletionRequestRow(Base):
    """One immutable request identity and its eventual response within a user scope."""

    __tablename__ = "completion_requests"
    __table_args__ = (
        sa.Index("ix_completion_requests_owner_session", "tenant_id", "user_id", "session_id"),
    )

    tenant_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.String(512), primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    response_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
