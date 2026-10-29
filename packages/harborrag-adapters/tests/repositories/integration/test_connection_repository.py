"""SqlConnectionRepository and the 0041 connection migration against SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from harborrag_adapters.repositories.database.control_plane.connections import (
    SqlConnectionRepository,
    SqlConnectionRevisionRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_adapters.repositories.database.control_plane.projects import SqlProjectRepository
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborPreconditionFailedError,
    HarborValidationError,
)
from harborrag_core.domain.connection import Connection, ConnectionBuilder
from harborrag_core.domain.connection_snapshot import ConnectionSnapshot
from harborrag_core.domain.project import Project

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]


async def _projects(sessions: SessionFactory, *pairs: tuple[str, str]) -> None:
    repository = SqlProjectRepository(sessions)
    for tenant_id, project_id in pairs:
        await repository.create(
            Project(id=project_id, tenant_id=tenant_id, name=project_id, collection=project_id)
        )


def _connection(
    connection_id: str, *, project_id: str = "cpm", tenant_id: str = "acme"
) -> Connection:
    return (
        ConnectionBuilder.for_new(
            tenant_id=tenant_id, project_id=project_id, connection_id=connection_id
        )
        .source_type("jira")
        .credentials(
            {
                "base_url": "https://acme.atlassian.net",
                "api_token": {"secret_ref": "secret://db/a"},
            }
        )
        .config({"jql": "project = CPM"})
        .tuning({"requests_per_minute": 600})
        .secret_refs(["secret://db/a"])
        .actor("u_huy")
        .build()
    )


@pytest.mark.asyncio
async def test_put_creates_then_overwrites_and_reads_back_every_field(
    sessions: SessionFactory,
) -> None:
    await _projects(sessions, ("acme", "cpm"))
    repository = SqlConnectionRepository(sessions)

    created, outcome = await repository.put(_connection("cpm-tickets"))
    assert (outcome, created.version) == ("created", 1)
    fetched = await repository.get(tenant_id="acme", project_id="cpm", connection_id="cpm-tickets")
    assert fetched == created
    assert await repository.get_by_source_key(tenant_id="acme", source_key="cpm/cpm-tickets") == (
        created
    )
    assert await repository.get_by_id(created.id, tenant_ids=frozenset({"acme"})) == created

    changed = (
        ConnectionBuilder.from_existing(created)
        .name("CPM tickets")
        .config({"jql": "project = HR"})
        .actor("u_ops")
        .build()
    )
    stored, outcome = await repository.put(changed, expected_version=1)

    assert (outcome, stored.version, stored.name) == ("overwritten", 2, "CPM tickets")
    assert (stored.created_by, stored.updated_by) == ("u_huy", "u_ops")
    assert stored.config == {"jql": "project = HR"}
    assert stored.tuning == {"requests_per_minute": 600}


@pytest.mark.asyncio
async def test_put_preconditions_map_to_precondition_failed(sessions: SessionFactory) -> None:
    await _projects(sessions, ("acme", "cpm"))
    repository = SqlConnectionRepository(sessions)
    created, _ = await repository.put(_connection("cpm-tickets"))

    with pytest.raises(HarborPreconditionFailedError, match="version does not match"):
        await repository.put(created, expected_version=7)
    with pytest.raises(HarborPreconditionFailedError, match="already exists"):
        await repository.put(created, create_only=True)
    with pytest.raises(HarborPreconditionFailedError, match="does not exist"):
        await repository.put(_connection("missing"), expected_version=1)


@pytest.mark.asyncio
async def test_concurrent_overwrites_at_one_version_let_exactly_one_win(
    sessions: SessionFactory,
) -> None:
    await _projects(sessions, ("acme", "cpm"))
    repository = SqlConnectionRepository(sessions)
    created, _ = await repository.put(_connection("cpm-tickets"))
    writers = [
        ConnectionBuilder.from_existing(created).name(f"writer {index}").build()
        for index in range(2)
    ]

    results = await asyncio.gather(
        *(repository.put(writer, expected_version=1) for writer in writers),
        return_exceptions=True,
    )

    assert sum(isinstance(result, HarborPreconditionFailedError) for result in results) == 1
    final = await repository.get(tenant_id="acme", project_id="cpm", connection_id="cpm-tickets")
    assert final is not None and final.version == 2


@pytest.mark.asyncio
async def test_source_key_is_unique_per_tenant(sessions: SessionFactory) -> None:
    await _projects(sessions, ("acme", "cpm"), ("acme", "hr"))
    repository = SqlConnectionRepository(sessions)
    await repository.put(_connection("tickets"))
    clash = (
        ConnectionBuilder.for_new(tenant_id="acme", project_id="hr", connection_id="other")
        .source_type("jira")
        .source_key("cpm/tickets")
        .build()
    )

    with pytest.raises(HarborConflictError):
        await repository.put(clash)


@pytest.mark.asyncio
async def test_list_pages_attention_first_and_filters(sessions: SessionFactory) -> None:
    await _projects(sessions, ("acme", "a"), ("acme", "b"), ("other", "z"))
    repository = SqlConnectionRepository(sessions)
    for project_id, connection_id in [("b", "one"), ("a", "two"), ("a", "one")]:
        await repository.put(_connection(connection_id, project_id=project_id))
    await repository.put(_connection("hidden", project_id="z", tenant_id="other"))
    await repository.set_status(
        tenant_id="acme", source_key="b/one", status="needs_reauth", detail={"code": "401"}
    )
    scope = frozenset({"acme"})

    first, cursor = await repository.list(tenant_ids=scope, limit=2)
    second, last = await repository.list(tenant_ids=scope, limit=2, cursor=cursor)

    assert [c.ref for c in first] == ["b/one", "a/one"]
    assert [c.ref for c in second] == ["a/two"]
    assert last is None
    assert first[0].status_detail == {"code": "401"} and first[0].version == 1
    filtered, _ = await repository.list(tenant_ids=scope, status="needs_reauth")
    assert [c.ref for c in filtered] == ["b/one"]
    by_project, _ = await repository.list(tenant_ids=scope, project_id="a")
    assert [c.ref for c in by_project] == ["a/one", "a/two"]
    with pytest.raises(HarborValidationError):
        await repository.list(tenant_ids=scope, cursor="garbage")


@pytest.mark.asyncio
async def test_scoped_reads_hide_other_tenants_and_delete_is_idempotent(
    sessions: SessionFactory,
) -> None:
    await _projects(sessions, ("acme", "cpm"), ("other", "z"))
    repository = SqlConnectionRepository(sessions)
    theirs, _ = await repository.put(_connection("theirs", project_id="z", tenant_id="other"))

    assert await repository.get_by_id(theirs.id, tenant_ids=frozenset({"acme"})) is None
    assert await repository.get(tenant_id="acme", project_id="z", connection_id="theirs") is None
    await repository.delete(tenant_id="other", project_id="z", connection_id="theirs")
    await repository.delete(tenant_id="other", project_id="z", connection_id="theirs")
    assert await repository.get_by_id(theirs.id, tenant_ids=None) is None


@pytest.mark.asyncio
async def test_set_status_honours_its_from_status_guard(sessions: SessionFactory) -> None:
    await _projects(sessions, ("acme", "cpm"))
    repository = SqlConnectionRepository(sessions)
    await repository.put(_connection("tickets"))
    key = {"tenant_id": "acme", "source_key": "cpm/tickets"}

    assert await repository.set_status(**key, status="error") is True
    assert (
        await repository.set_status(**key, status="active", only_from=frozenset({"needs_reauth"}))
        is False
    )
    assert await repository.set_status(**key, status="active", only_from=frozenset({"error"}))
    stored = await repository.get_by_source_key(**key)
    assert stored is not None and stored.status == "active" and stored.version == 1


@pytest.mark.asyncio
async def test_revisions_are_saved_once_per_fingerprint_and_listed_per_connection(
    sessions: SessionFactory,
) -> None:
    revisions = SqlConnectionRevisionRepository(sessions)
    first = ConnectionSnapshot(
        tenant_id="acme",
        source_key="cpm/tickets",
        source_type="jira",
        credentials={"api_token": {"secret_ref": "secret://db/a", "last4": "9f2c"}},
        config={"jql": "project = CPM"},
    )
    second = ConnectionSnapshot(
        tenant_id="acme", source_key="cpm/tickets", source_type="jira", config={"jql": "x"}
    )

    await revisions.save(first)
    await revisions.save(first)
    await revisions.save(second)

    assert await revisions.get(tenant_id="acme", fingerprint=first.fingerprint) == first
    assert await revisions.get(tenant_id="other", fingerprint=first.fingerprint) is None
    listed = await revisions.list_for_source(tenant_id="acme", source_key="cpm/tickets")
    assert [snapshot.fingerprint for snapshot in listed] == [first.fingerprint, second.fingerprint]
    assert listed[0].secret_refs == ["secret://db/a"]


_LEGACY_ROWS = [
    ("s1", "acme", "cpm", "jira_cloud", "CPM Tickets", "active"),
    ("s2", "acme", "cpm", "jira", "CPM tickets", "paused"),
    ("s3", "acme", "hr", "local", "CPM Tickets", "active"),
    ("s4", "acme", "cpm", "notion", "Wiki", "active"),
]


def _seed_legacy_sources(database: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for project_id in ("cpm", "hr"):
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, tenant_id, name, description, collection, status,"
                    " created_at, updated_at, documents, chunks, size_bytes)"
                    " VALUES (:id, 'acme', :id, '', :id, 'active', '2026-01-01', '2026-01-01',"
                    " 0, 0, 0)"
                ),
                {"id": project_id},
            )
        for source_id, tenant_id, project_id, source_type, name, status in _LEGACY_ROWS:
            connection.execute(
                sa.text(
                    "INSERT INTO sources (id, tenant_id, project_id, source_type, name,"
                    " config_json, secret_refs, status, documents, chunks, errors)"
                    " VALUES (:id, :tenant, :project, :type, :name, :config, '[]', :status,"
                    " 0, 0, 0)"
                ),
                {
                    "id": source_id,
                    "tenant": tenant_id,
                    "project": project_id,
                    "type": source_type,
                    "name": name,
                    "status": status,
                    "config": (
                        '{"base_url": "https://acme.atlassian.net", "token": {"secret_ref":'
                        ' "secret://db/t"}, "project_keys": ["CPM"], "page_size": 50}'
                    ),
                },
            )
    engine.dispose()


def _rows(database: Path) -> dict[str, dict[str, object]]:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            result = connection.execute(sa.text("SELECT * FROM sources ORDER BY id")).mappings()
            return {str(row["id"]): dict(row) for row in result}
    finally:
        engine.dispose()


def test_0041_backfills_unique_connection_ids_and_splits_legacy_config(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    config = _build_config(dsn)
    command.upgrade(config, "0040")
    _seed_legacy_sources(database)

    run_migrations(dsn)
    rows = _rows(database)

    assert [rows[key]["connection_id"] for key in ("s1", "s2", "s3")] == [
        "cpm-tickets",
        "cpm-tickets-2",
        "cpm-tickets",
    ]
    assert rows["s1"]["source_key"] == "cpm/cpm-tickets"
    assert rows["s3"]["source_key"] == "hr/cpm-tickets"
    assert rows["s1"]["source_type"] == "jira" and rows["s3"]["source_type"] == "local_file"
    assert rows["s2"]["status"] == "active" and rows["s2"]["version"] == 1
    assert '"api_token": {"secret_ref": "secret://db/t"}' in str(rows["s1"]["credentials"])
    assert '"method": "pat"' in str(rows["s1"]["credentials"])
    assert '"project_keys": ["CPM"]' in str(rows["s1"]["config_json"])
    assert '"page_size": 50' in str(rows["s1"]["tuning"])
    assert rows["s4"]["status"] == "error" and "unknown source type" in str(
        rows["s4"]["status_detail"]
    )

    command.downgrade(config, "0040")
    restored = _rows(database)
    assert restored["s1"]["source_type"] == "jira"
    assert restored["s3"]["source_type"] == "local"
    assert '"token": {"secret_ref": "secret://db/t"}' in str(restored["s1"]["config_json"])
    assert '"page_size": 50' in str(restored["s1"]["config_json"])
    assert "connection_id" not in restored["s1"]
    assert "method" not in str(restored["s1"]["config_json"])
