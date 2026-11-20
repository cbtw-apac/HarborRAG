"""One contract for every TenantDirectory: the in-memory fake and the SQL adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.ingestion_control.schema import SOURCE_SCOPES
from harborrag_adapters.repositories.database.ingestion_control.tenant_directory import (
    SqlTenantDirectory,
)
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ports.tenant_directory import TenantDirectory
from harborrag_core.schemas.ids import TenantId

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

type DirectoryFactory = Callable[[list[str]], Awaitable[TenantDirectory]]


class InMemoryTenantDirectory:
    """TenantDirectory over the tenants that own a fixed list of source scopes."""

    def __init__(self, scope_tenants: list[str]) -> None:
        self._scope_tenants = scope_tenants

    async def tenant_ids(self) -> list[TenantId]:
        return [TenantId(tenant) for tenant in sorted(set(self._scope_tenants))]


def _migrated_engine(tmp_path: Path) -> AsyncEngine:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    return create_control_plane_engine(dsn)


async def _add_source_scopes(engine: AsyncEngine, scope_tenants: list[str]) -> None:
    rows = [
        {
            "source_scope_id": f"scope-{index}",
            "tenant_id": tenant,
            "connector_type": "localfile",
            "connection_id": f"connection-{index}",
            "configuration_fingerprint": "fingerprint",
            "created_at": NOW,
            "updated_at": NOW,
        }
        for index, tenant in enumerate(scope_tenants)
    ]
    if rows:
        async with engine.begin() as connection:
            await connection.execute(SOURCE_SCOPES.insert(), rows)


@pytest_asyncio.fixture(
    params=[
        pytest.param("in_memory", marks=pytest.mark.unit),
        pytest.param("sql", marks=pytest.mark.contract),
    ]
)
async def directory_for(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[DirectoryFactory]:
    """Build a directory whose source scopes belong to the given tenants."""
    if request.param == "in_memory":

        async def in_memory(scope_tenants: list[str]) -> TenantDirectory:
            return InMemoryTenantDirectory(scope_tenants)

        yield in_memory
        return
    engine = _migrated_engine(tmp_path)

    async def sql(scope_tenants: list[str]) -> TenantDirectory:
        await _add_source_scopes(engine, scope_tenants)
        return SqlTenantDirectory(engine)

    yield sql
    await engine.dispose()


@pytest.mark.asyncio
async def test_directory_lists_each_tenant_once_in_order(directory_for: DirectoryFactory) -> None:
    directory = await directory_for(["tenant-b", "tenant-a", "tenant-a"])

    assert await directory.tenant_ids() == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_directory_is_empty_without_source_scopes(directory_for: DirectoryFactory) -> None:
    directory = await directory_for([])

    assert await directory.tenant_ids() == []


@pytest.mark.contract
@pytest.mark.asyncio
async def test_sql_directory_stops_at_its_limit(tmp_path: Path) -> None:
    engine = _migrated_engine(tmp_path)
    try:
        await _add_source_scopes(engine, ["tenant-a", "tenant-b", "tenant-c"])

        listed = await SqlTenantDirectory(engine, limit=2).tenant_ids()
    finally:
        await engine.dispose()

    assert listed == ["tenant-a", "tenant-b"]


@pytest.mark.contract
@pytest.mark.asyncio
async def test_sql_directory_reports_an_unreadable_store_as_unavailable(tmp_path: Path) -> None:
    engine = create_control_plane_engine(f"sqlite+aiosqlite:///{tmp_path}/not-migrated.db")
    try:
        with pytest.raises(HarborUnavailableError):
            await SqlTenantDirectory(engine).tenant_ids()
    finally:
        await engine.dispose()
