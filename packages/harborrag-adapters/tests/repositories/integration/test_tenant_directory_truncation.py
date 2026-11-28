"""The SQL tenant directory says so when its limit cuts the listing short."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.ingestion_control.schema import SOURCE_SCOPES
from harborrag_adapters.repositories.database.ingestion_control.tenant_directory import (
    SqlTenantDirectory,
)

pytestmark = pytest.mark.contract

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
LOGGER = "harborrag.adapters.ingestion_control.tenant_directory"


async def _engine_with_tenants(tmp_path: Path, tenants: list[str]) -> AsyncEngine:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    async with engine.begin() as connection:
        await connection.execute(
            SOURCE_SCOPES.insert(),
            [
                {
                    "source_scope_id": f"scope-{index}",
                    "tenant_id": tenant,
                    "connector_type": "localfile",
                    "connection_id": f"connection-{index}",
                    "configuration_fingerprint": "fingerprint",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for index, tenant in enumerate(tenants)
            ],
        )
    return engine


@pytest.mark.asyncio
async def test_tenant_ids_past_the_limit_logs_the_truncation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = await _engine_with_tenants(tmp_path, ["tenant-a", "tenant-b", "tenant-c"])
    try:
        with caplog.at_level("WARNING", logger=LOGGER):
            listed = await SqlTenantDirectory(engine, limit=2).tenant_ids()
    finally:
        await engine.dispose()

    assert listed == ["tenant-a", "tenant-b"]
    assert [record.getMessage() for record in caplog.records] == [
        "Tenant directory listing truncated limit=2"
    ]
    assert "tenant-c" not in caplog.text


@pytest.mark.asyncio
async def test_tenant_ids_exactly_at_the_limit_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = await _engine_with_tenants(tmp_path, ["tenant-a", "tenant-b"])
    try:
        with caplog.at_level("WARNING", logger=LOGGER):
            listed = await SqlTenantDirectory(engine, limit=2).tenant_ids()
    finally:
        await engine.dispose()

    assert listed == ["tenant-a", "tenant-b"]
    assert caplog.records == []
