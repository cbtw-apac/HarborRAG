"""SqlLeaseRepository: LeaseRepositoryPort over the singleton_leases table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime

from .schemas import SingletonLeaseRow
from .session import SessionFactory

_LAPSED = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class SqlLeaseRepository:
    """LeaseRepositoryPort over singleton_leases."""

    sessions: SessionFactory

    async def try_acquire(self, name: str, holder: str, *, ttl_seconds: float) -> bool:
        """Acquire or renew ``name`` for ``holder`` with one conditional UPDATE.

        Not a read-then-write: the WHERE clause (already this holder, or the
        current holder's lease has lapsed) is evaluated atomically by the
        database, so two processes racing this at once can never both
        believe they hold the lease. A ``name`` with no row yet, such as a
        per-connection sync lease, first gets an already-lapsed row.

        Both the expiry check and the new expiry value are anchored to the
        *database's* clock (``_server_now``), never this process's own: if
        this compared against ``datetime.now()`` here, a contender whose
        local clock merely runs fast relative to the actual holder's process
        could believe a still-live lease had already lapsed and steal it,
        letting two holders run at once -- exactly what this lease exists to
        prevent.
        """
        async with self.sessions.begin() as session:
            await self._ensure_row(session, name)
            now = await self._server_now(session)
            expires_at = now + timedelta(seconds=ttl_seconds)
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

    @staticmethod
    async def _ensure_row(session: AsyncSession, name: str) -> None:
        """Insert a lapsed, unheld row for ``name`` unless one exists."""
        factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
        await session.execute(
            factory(SingletonLeaseRow)
            .values(name=name, holder="", expires_at=_LAPSED)
            .on_conflict_do_nothing(index_elements=["name"])
        )

    @staticmethod
    async def _server_now(session: AsyncSession) -> datetime:
        """The database server's current UTC time, not this process's.

        ``type_coerce`` (not ``cast``) is deliberate: casting ``now()`` to a
        SQL datetime type under SQLite hits its NUMERIC-affinity CAST rule,
        which silently truncates a text timestamp at its first non-digit
        character instead of parsing it. ``type_coerce`` renders no SQL at
        all -- it only tells SQLAlchemy to run the raw driver value through
        ``UTCDateTime``'s Python-side result processor, which already
        parses SQLite's naive text timestamp (and normalizes Postgres's
        native aware one) the same way every mapped ``UTCDateTime`` column
        in this codebase does.
        """
        result = await session.execute(sa.select(sa.type_coerce(sa.func.now(), UTCDateTime())))
        return result.scalar_one()
