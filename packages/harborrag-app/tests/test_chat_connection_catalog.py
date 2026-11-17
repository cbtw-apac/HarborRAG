"""The production connection catalog reads a project's connections and their ingestion records."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.chat.scope import (
    ConnectionSources,
    ControlPlaneConnectionCatalog,
)
from harborrag_core.domain.connection import Connection
from harborrag_core.testing.control_plane_fakes import FakeConnectionRepository

pytestmark = pytest.mark.unit


class _Records:
    """Source scopes and published state keyed by a connection's ``source_key``, read in batches."""

    def __init__(self) -> None:
        self.scopes = {"legacy-jira": ["scope-a", "scope-b"]}
        self.published = {"legacy-jira"}
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    async def source_scope_ids_by_connection(
        self, *, tenant_id: str, connection_ids: list[str]
    ) -> dict[str, list[str]]:
        self.asked.append(("scopes", tuple(connection_ids)))
        return {key: self.scopes.get(key, []) for key in connection_ids}

    async def published_connection_ids(
        self, *, tenant_id: str, connection_ids: list[str]
    ) -> set[str]:
        self.asked.append(("published", tuple(connection_ids)))
        return self.published & set(connection_ids)


def _connection(project_id: str, connection_id: str, source_key: str, tenant: str = "ACME"):
    return Connection(
        id=f"{tenant}-{project_id}-{connection_id}",
        tenant_id=tenant,
        project_id=project_id,
        connection_id=connection_id,
        source_key=source_key,
        source_type="jira",
        name=connection_id,
    )


@pytest.mark.asyncio
async def test_a_projects_connections_are_read_by_their_source_key() -> None:
    repository = FakeConnectionRepository()
    for connection in (
        _connection("cpm", "tickets", "legacy-jira"),
        _connection("cpm", "wiki", "cpm/wiki"),
        _connection("other", "board", "other/board"),
        _connection("cpm", "tickets", "globex-jira", tenant="GLOBEX"),
    ):
        repository.connections[connection.id] = connection
    records = _Records()

    async def index() -> _Records:
        return records

    catalog = ControlPlaneConnectionCatalog(lambda: repository, index)  # type: ignore[arg-type]

    found = await catalog.project_connections("ACME", "cpm")

    assert found == (
        ConnectionSources("cpm/tickets", ("scope-a", "scope-b"), published=True),
        ConnectionSources("cpm/wiki", (), published=False),
    )
    assert records.asked == [
        ("scopes", ("legacy-jira", "cpm/wiki")),
        ("published", ("legacy-jira", "cpm/wiki")),
    ]
