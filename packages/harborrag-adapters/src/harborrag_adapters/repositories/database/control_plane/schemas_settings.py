"""Runtime-settings ORM rows (migration 0046).

Kept in its own module and imported from schemas.py so the tables register on
the shared ``Base`` metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import Base, JSONVariant


class SettingOverrideRow(Base):
    """One stored override of a tenant at a level, stamped with the revision that set it."""

    __tablename__ = "setting_overrides"

    tenant_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    level: Mapped[str] = mapped_column(sa.String(300), primary_key=True)
    setting_key: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_by: Mapped[str] = mapped_column(sa.String(256), nullable=False)


class SettingRevisionRow(Base):
    """One append-only revision of a tenant's settings with its recorded changes."""

    __tablename__ = "setting_revisions"

    tenant_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    changes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False)

    __table_args__ = (
        sa.CheckConstraint("source IN ('seed','import','api')", name="source"),
        sa.CheckConstraint("revision >= 1", name="revision"),
    )
