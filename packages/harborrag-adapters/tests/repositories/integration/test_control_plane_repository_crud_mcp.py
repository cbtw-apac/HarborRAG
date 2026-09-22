"""SqlMcpQueryLogRepository/SqlMcpConfigSnapshotRepository round-trips on SQLite.

Split out of test_control_plane_repository_crud.py to keep that file under
the repo's file-length gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.mcp_config_snapshot import (
    SqlMcpConfigSnapshotRepository,
)
from harborrag_adapters.repositories.database.control_plane.mcp_query_log import (
    SqlMcpQueryLogRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.domain.mcp_usage import McpConfigSnapshot, McpUsageEntry

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    """Migrated SQLite-file DB and a session factory, torn down per test."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


def _entry(
    *,
    tool: str,
    client: str,
    latency_ms: int,
    created_at: datetime,
) -> McpUsageEntry:
    return McpUsageEntry(tool=tool, client=client, latency_ms=latency_ms, created_at=created_at)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_mcp_query_log_repository_records_and_pings(sessions: SessionFactory) -> None:
    repo = SqlMcpQueryLogRepository(sessions)

    assert await repo.ping() is True

    await repo.record(
        _entry(
            tool="vector_search",
            client="account-1",
            latency_ms=10,
            created_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
        )
    )

    entries = await repo.list_since(since=datetime(2026, 1, 1, tzinfo=UTC), limit=10)
    assert len(entries) == 1
    assert entries[0].tool == "vector_search"
    assert entries[0].client == "account-1"
    assert entries[0].latency_ms == 10


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_mcp_query_log_repository_rolls_up_usage_by_client_and_tool(
    sessions: SessionFactory,
) -> None:
    repo = SqlMcpQueryLogRepository(sessions)
    await repo.record(
        _entry(
            tool="vector_search",
            client="account-1",
            latency_ms=10,
            created_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
        )
    )
    await repo.record(
        _entry(
            tool="vector_search",
            client="account-1",
            latency_ms=20,
            created_at=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
        )
    )
    await repo.record(
        _entry(
            tool="graph_search",
            client="account-2",
            latency_ms=5,
            created_at=datetime(2026, 8, 12, 0, 0, 2, tzinfo=UTC),
        )
    )

    clients = await repo.usage_by_client()
    by_client = {usage.client: usage for usage in clients}
    assert by_client["account-1"].query_count == 2
    assert by_client["account-1"].last_seen_at == datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC)
    assert by_client["account-2"].query_count == 1

    tools = await repo.usage_by_tool()
    by_tool = {usage.tool: usage for usage in tools}
    assert by_tool["vector_search"].call_count == 2
    assert by_tool["vector_search"].avg_latency_ms == 15.0
    assert by_tool["graph_search"].call_count == 1
    assert by_tool["graph_search"].avg_latency_ms == 5.0


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_mcp_query_log_repository_list_since_filters_and_orders_newest_first(
    sessions: SessionFactory,
) -> None:
    repo = SqlMcpQueryLogRepository(sessions)
    for i in range(3):
        await repo.record(
            _entry(
                tool="vector_search",
                client="account-1",
                latency_ms=i,
                created_at=datetime(2026, 8, 12, 0, 0, i, tzinfo=UTC),
            )
        )

    recent = await repo.list_since(
        since=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
        limit=10,
    )
    assert [entry.latency_ms for entry in recent] == [2, 1]

    limited = await repo.list_since(since=datetime(2026, 1, 1, tzinfo=UTC), limit=1)
    assert len(limited) == 1
    assert limited[0].latency_ms == 2


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_mcp_config_snapshot_repository_is_empty_until_published(
    sessions: SessionFactory,
) -> None:
    repo = SqlMcpConfigSnapshotRepository(sessions)

    assert await repo.get() is None

    snapshot = McpConfigSnapshot(
        policy={"max_results": 20, "allow_ingestion": False},
        disabled_tools=("ingestion_run",),
        enabled_tool_count=4,
        total_tool_count=5,
        revision="rev-1",
        restart_required=False,
        updated_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
    )
    await repo.put(snapshot)

    fetched = await repo.get()
    assert fetched == snapshot


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_mcp_config_snapshot_repository_put_replaces_the_prior_snapshot(
    sessions: SessionFactory,
) -> None:
    repo = SqlMcpConfigSnapshotRepository(sessions)
    first = McpConfigSnapshot(
        policy={"max_results": 20, "allow_ingestion": False},
        disabled_tools=(),
        enabled_tool_count=5,
        total_tool_count=5,
        revision="rev-1",
        restart_required=False,
        updated_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
    )
    second = McpConfigSnapshot(
        policy={"max_results": 10, "allow_ingestion": True},
        disabled_tools=("ingestion_run",),
        enabled_tool_count=4,
        total_tool_count=5,
        revision="rev-2",
        restart_required=True,
        updated_at=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
    )

    await repo.put(first)
    await repo.put(second)

    fetched = await repo.get()
    assert fetched == second
