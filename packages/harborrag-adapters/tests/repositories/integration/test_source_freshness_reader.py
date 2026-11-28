"""The SQL source freshness reader against a migrated SQLite control database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    INGESTION_TASKS,
    SOURCE_SCOPES,
)
from harborrag_adapters.repositories.database.ingestion_control.source_freshness import (
    SqlSourceFreshnessReader,
)
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.ports.source_freshness import UNKNOWN_CONNECTOR_TYPE, SourceFreshness

pytestmark = pytest.mark.contract

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Run:
    task_id: str
    tenant_id: str
    scope: str
    status: IngestionTaskState
    submitted_minute: int
    completed_minute: int | None = None


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A migrated SQLite control database."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield engine
    await engine.dispose()


async def _add_scope(engine: AsyncEngine, tenant_id: str, scope: str, connector_type: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            SOURCE_SCOPES.insert(),
            {
                "source_scope_id": scope,
                "tenant_id": tenant_id,
                "connector_type": connector_type,
                "connection_id": f"connection-{scope}",
                "configuration_fingerprint": "fingerprint",
                "created_at": NOW,
                "updated_at": NOW,
            },
        )


async def _add_runs(engine: AsyncEngine, *runs: _Run) -> None:
    rows = [
        {
            "task_id": run.task_id,
            "tenant_id": run.tenant_id,
            "source_scope_id": run.scope,
            "status": run.status.value,
            "submitted_at": NOW + timedelta(minutes=run.submitted_minute),
            "completed_at": (
                None
                if run.completed_minute is None
                else NOW + timedelta(minutes=run.completed_minute)
            ),
            "request": {},
            "summary": {},
        }
        for run in runs
    ]
    async with engine.begin() as connection:
        await connection.execute(INGESTION_TASKS.insert(), rows)


@pytest.mark.asyncio
async def test_a_source_reports_its_last_success_and_its_latest_submitted_run(
    engine: AsyncEngine,
) -> None:
    await _add_scope(engine, "tenant-a", "scope-jira", "jira")
    await _add_runs(
        engine,
        _Run("completed", "tenant-a", "scope-jira", IngestionTaskState.COMPLETED, 0, 5),
        _Run("partial", "tenant-a", "scope-jira", IngestionTaskState.PARTIAL, 10, 40),
        _Run("failed", "tenant-a", "scope-jira", IngestionTaskState.FAILED, 20, 21),
    )

    report = await SqlSourceFreshnessReader(engine).source_freshness(limit=10)

    assert report.sources == (
        SourceFreshness(
            tenant_id="tenant-a",
            source_scope_id="scope-jira",
            connector_type="jira",
            last_success_at=NOW + timedelta(minutes=40),
            last_run_status=IngestionTaskState.FAILED,
        ),
    )
    assert report.sources[0].last_success_at is not None
    assert report.sources[0].last_success_at.utcoffset() == timedelta(0)
    assert report.source_count == 1
    assert not report.truncated


@pytest.mark.asyncio
async def test_the_latest_run_is_the_last_submitted_not_the_last_completed(
    engine: AsyncEngine,
) -> None:
    await _add_scope(engine, "tenant-a", "scope-slow", "local")
    await _add_runs(
        engine,
        _Run("slow", "tenant-a", "scope-slow", IngestionTaskState.COMPLETED, 0, 90),
        _Run("quick", "tenant-a", "scope-slow", IngestionTaskState.CANCELLED, 30, 31),
    )

    [source] = (await SqlSourceFreshnessReader(engine).source_freshness(limit=10)).sources

    assert source.last_run_status is IngestionTaskState.CANCELLED
    assert source.last_success_at == NOW + timedelta(minutes=90)


@pytest.mark.asyncio
async def test_a_source_that_never_succeeded_has_no_last_success(engine: AsyncEngine) -> None:
    await _add_scope(engine, "tenant-a", "scope-new", "local")
    await _add_runs(
        engine,
        _Run("failed", "tenant-a", "scope-new", IngestionTaskState.FAILED, 0, 1),
        _Run("pending", "tenant-a", "scope-new", IngestionTaskState.PENDING, 5),
    )

    [source] = (await SqlSourceFreshnessReader(engine).source_freshness(limit=10)).sources

    assert source.last_success_at is None
    assert source.last_run_status is IngestionTaskState.PENDING


@pytest.mark.asyncio
async def test_sources_of_every_tenant_are_read_stalest_first(engine: AsyncEngine) -> None:
    await _add_scope(engine, "tenant-a", "scope-recent", "local")
    await _add_scope(engine, "tenant-b", "scope-old", "local")
    await _add_scope(engine, "tenant-b", "scope-never", "local")
    await _add_runs(
        engine,
        _Run("recent", "tenant-a", "scope-recent", IngestionTaskState.COMPLETED, 0, 30),
        _Run("old", "tenant-b", "scope-old", IngestionTaskState.COMPLETED, 0, 10),
        _Run("never", "tenant-b", "scope-never", IngestionTaskState.RUNNING, 0),
    )

    report = await SqlSourceFreshnessReader(engine).source_freshness(limit=10)

    assert [(source.tenant_id, source.source_scope_id) for source in report.sources] == [
        ("tenant-b", "scope-never"),
        ("tenant-b", "scope-old"),
        ("tenant-a", "scope-recent"),
    ]


@pytest.mark.asyncio
async def test_the_limit_keeps_the_stalest_sources_and_counts_them_all(
    engine: AsyncEngine,
) -> None:
    for index in range(3):
        await _add_scope(engine, "tenant-a", f"scope-{index}", "local")
    await _add_runs(
        engine,
        *(
            _Run(
                f"run-{index}", "tenant-a", f"scope-{index}", IngestionTaskState.COMPLETED, 0, index
            )
            for index in range(3)
        ),
    )

    report = await SqlSourceFreshnessReader(engine).source_freshness(limit=2)

    assert [source.source_scope_id for source in report.sources] == ["scope-0", "scope-1"]
    assert report.source_count == 3
    assert report.truncated


@pytest.mark.asyncio
async def test_a_source_without_its_scope_record_has_an_unknown_connector_type(
    engine: AsyncEngine,
) -> None:
    await _add_runs(
        engine, _Run("orphan", "tenant-a", "scope-gone", IngestionTaskState.CANCELLED, 0, 1)
    )

    [source] = (await SqlSourceFreshnessReader(engine).source_freshness(limit=10)).sources

    assert source.connector_type == UNKNOWN_CONNECTOR_TYPE


@pytest.mark.asyncio
async def test_no_runs_read_as_no_sources(engine: AsyncEngine) -> None:
    report = await SqlSourceFreshnessReader(engine).source_freshness(limit=10)

    assert report.sources == ()
    assert report.source_count == 0


@pytest.mark.asyncio
async def test_an_unreadable_store_is_unavailable(tmp_path: Path) -> None:
    engine = create_control_plane_engine(f"sqlite+aiosqlite:///{tmp_path}/not-migrated.db")
    try:
        with pytest.raises(HarborUnavailableError):
            await SqlSourceFreshnessReader(engine).source_freshness(limit=10)
    finally:
        await engine.dispose()
