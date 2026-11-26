"""One connection row that fails validation does not break the connection listing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.connections import (
    SqlConnectionRepository,
)
from harborrag_adapters.repositories.database.control_plane.projects import SqlProjectRepository
from harborrag_adapters.repositories.database.control_plane.schemas import SourceRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.domain.connection import ConnectionBuilder
from harborrag_core.domain.project import Project

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
LOGGER = "harborrag.adapters.control_plane.connections"


async def _seed(sessions: SessionFactory) -> SqlConnectionRepository:
    await SqlProjectRepository(sessions).create(
        Project(id="demo", tenant_id="acme", name="Demo", collection="demo")
    )
    repository = SqlConnectionRepository(sessions)
    await repository.put(
        ConnectionBuilder.for_new(tenant_id="acme", project_id="demo", connection_id="b-good")
        .source_type("jira")
        .credentials({"api_token": {"secret_ref": "secret://db/a"}})
        .secret_refs(["secret://db/a"])
        .build()
    )
    async with sessions.begin() as session:
        await session.execute(
            sa.insert(SourceRow).values(
                id="pk-bad",
                tenant_id="acme",
                project_id="demo",
                connection_id="a-bad",
                source_key="demo/a-bad",
                source_type="jira",
                name="a-bad",
                credentials={"token": "demo-token-000"},
                config_json={},
                tuning={},
                secret_refs=[],
                status="active",
                version=1,
                created_by="system",
                updated_by="system",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return repository


@pytest.mark.asyncio
async def test_list_with_an_invalid_row_skips_and_logs_it(
    sessions: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    repository = await _seed(sessions)

    with caplog.at_level("WARNING", logger=LOGGER):
        connections, cursor = await repository.list(tenant_ids=None)

    assert [connection.connection_id for connection in connections] == ["b-good"]
    assert cursor is None
    assert "connection_pk=pk-bad" in caplog.text
    assert "demo-token-000" not in caplog.text


@pytest.mark.asyncio
async def test_list_pages_past_an_invalid_row_without_repeating_it(
    sessions: SessionFactory,
) -> None:
    repository = await _seed(sessions)

    first, cursor = await repository.list(tenant_ids=None, limit=1)
    second, last = await repository.list(tenant_ids=None, limit=1, cursor=cursor)

    assert first == [] and cursor is not None
    assert [connection.connection_id for connection in second] == ["b-good"]
    assert last is None
