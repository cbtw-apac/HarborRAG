from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.backends.sqlite import sqlite_url
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

_MARKER = "demo-parameter-value"


async def _duplicate_insert_error(engine: AsyncEngine) -> str:
    async with engine.begin() as connection:
        await connection.execute(sa.text("CREATE TABLE demo (key TEXT PRIMARY KEY)"))
        await connection.execute(sa.text("INSERT INTO demo (key) VALUES (:key)"), {"key": _MARKER})
    with pytest.raises(IntegrityError) as failure:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("INSERT INTO demo (key) VALUES (:key)"), {"key": _MARKER}
            )
    return str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dsn",
    ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///{tmp}/control.db"],
)
async def test_control_plane_engine_keeps_sql_parameters_out_of_errors(
    dsn: str, tmp_path: object
) -> None:
    engine = create_control_plane_engine(dsn.format(tmp=tmp_path))
    try:
        message = await _duplicate_insert_error(engine)
    finally:
        await engine.dispose()

    assert _MARKER not in message


def test_postgres_control_plane_engine_hides_sql_parameters() -> None:
    engine = create_control_plane_engine(
        "postgresql+asyncpg://demo:demo@127.0.0.1:5432/demo", pool_pre_ping=True
    )

    assert engine.sync_engine.hide_parameters is True


@pytest.mark.asyncio
async def test_sqlalchemy_client_keeps_sql_parameters_out_of_errors() -> None:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=sqlite_url(":memory:"),
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1800,
        echo=False,
    )
    await client.connect()
    try:
        message = await _duplicate_insert_error(client.raw)
    finally:
        await client.close()

    assert _MARKER not in message
