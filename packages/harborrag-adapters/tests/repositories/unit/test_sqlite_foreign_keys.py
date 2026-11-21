"""SQLite engines enforce foreign keys, so ON DELETE CASCADE removes child rows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.ingestion_control.connection_records import (
    ConnectionRecordRepository,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    DOCUMENT_VERSIONS,
    DOCUMENTS,
    METADATA,
    SOURCE_SCOPES,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def _row(table: sa.Table, **given: object) -> dict[str, object]:
    """Fill every required column of ``table`` with a placeholder, then apply ``given``."""
    row: dict[str, object] = {}
    for column in table.columns:
        if column.name in given or column.nullable:
            continue
        if isinstance(column.type, sa.Boolean):
            row[column.name] = True
        elif isinstance(column.type, sa.Integer):
            row[column.name] = 0
        elif isinstance(column.type, sa.JSON):
            row[column.name] = {}
        elif "DateTime" in type(column.type).__name__:
            row[column.name] = NOW
        else:
            row[column.name] = "x"
    return {**row, **given}


@pytest.mark.asyncio
async def test_purge_on_sqlite_removes_the_document_versions_of_purged_documents(
    tmp_path: Path,
) -> None:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=f"sqlite+aiosqlite:///{tmp_path}/cascade.db",
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1800,
        echo=False,
    )
    await client.connect()
    try:
        await client.create_schema(METADATA)
        async with client.sessions.begin() as session:
            await session.execute(
                sa.insert(SOURCE_SCOPES).values(
                    **_row(SOURCE_SCOPES, source_scope_id="sc1", tenant_id="t1", connection_id="c1")
                )
            )
            await session.execute(
                sa.insert(DOCUMENTS).values(
                    **_row(
                        DOCUMENTS,
                        document_id="d1",
                        tenant_id="t1",
                        connection_id="c1",
                        source_scope_id="sc1",
                    )
                )
            )
            await session.execute(
                sa.insert(DOCUMENT_VERSIONS).values(
                    **_row(
                        DOCUMENT_VERSIONS,
                        document_version_id="v1",
                        document_id="d1",
                        source_scope_id="sc1",
                        status="RETIRED",
                    )
                )
            )

        counts = await ConnectionRecordRepository(client).purge(tenant_id="t1", connection_id="c1")

        async with client.sessions() as session:
            left = await session.scalar(sa.select(sa.func.count()).select_from(DOCUMENT_VERSIONS))
    finally:
        await client.close()
    assert (counts.documents, counts.source_scopes) == (1, 1)
    assert left == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("database", ["file", "memory"])
async def test_control_plane_sqlite_engine_enforces_foreign_keys(
    tmp_path: Path, database: str
) -> None:
    dsn = (
        f"sqlite+aiosqlite:///{tmp_path}/control.db"
        if database == "file"
        else "sqlite+aiosqlite:///:memory:"
    )
    engine = create_control_plane_engine(dsn)
    try:
        async with engine.connect() as connection:
            enabled = await connection.scalar(sa.text("PRAGMA foreign_keys"))
    finally:
        await engine.dispose()
    assert enabled == 1
