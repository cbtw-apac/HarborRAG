"""SqlLeaseRepository: LeaseRepositoryPort over the singleton_leases table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from harborrag_core.base import utc_now

from .schemas import SingletonLeaseRow
from .session import SessionFactory


@dataclass(slots=True)
class SqlLeaseRepository:
    """LeaseRepositoryPort over singleton_leases."""

    sessions: SessionFactory

    async def try_acquire(self, name: str, holder: str, *, ttl_seconds: float) -> bool:
        """Acquire or renew ``name`` for ``holder`` with one conditional UPDATE.

        Not a read-then-write: the WHERE clause (already this holder, or the
        current holder's lease has lapsed) is evaluated atomically by the
        database, so two processes racing this at once can never both
        believe they hold the lease. The row must already exist (seeded by
        this lease's migration) -- an unknown ``name`` always returns False.
        """
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        async with self.sessions.begin() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    sa.update(SingletonLeaseRow)
                    .where(
                        SingletonLeaseRow.name == name,
                        sa.or_(
                            SingletonLeaseRow.holder == holder,
                            SingletonLeaseRow.expires_at <= now,
                        ),
                    )
                    .values(holder=holder, expires_at=expires_at)
                ),
            )
            return result.rowcount == 1
