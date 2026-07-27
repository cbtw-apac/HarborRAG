"""Alembic environment for the control-plane DB: async engine, typed metadata."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from harborrag_adapters.repositories.database.control_plane.schemas import Base

target_metadata = Base.metadata


def _database_url() -> str:
    """DSN injected programmatically by migrations.run_migrations."""
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url not configured for control-plane alembic")
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (--sql mode)."""
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Run the migration context on a sync-facade connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Connect with the async driver and run migrations through run_sync."""
    engine = create_async_engine(_database_url())
    async with engine.begin() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async_migrations())
