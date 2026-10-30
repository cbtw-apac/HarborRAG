"""Importing connectors.yaml into connections, once, and exporting them back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harborrag_core.domain.connection import ConnectionBuilder
from harborrag_core.domain.project import Project
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeConnectionRevisionRepository,
    FakeProjectRepository,
    FakeSettingsRepository,
)
from harborrag_runtime.config.connectors.importing import (
    IMPORT_MARKER_KEY,
    ConnectionImportService,
    import_connectors_once,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.execution.source_submission import prepare_source_submission
from harborrag_runtime.ingestion.connections import ConnectionResolver
from harborrag_runtime.ingestion_contracts import SourceSubmission

from .conftest import REPO_ROOT

_LEGACY = """
version: 1
connectors:
  jira-main:
    provider: jira
    environment:
      base_url: JIRA_BASE_URL
    settings:
      project_keys: [ENG]
      requests_per_minute: 600
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
  Wiki.Main:
    provider: confluence
    project_id: wiki
    settings:
      base_url: https://acme.atlassian.net/wiki
      space_key: ENG
    secrets:
      token_env: CONFLUENCE_TOKEN
      email_env: CONFLUENCE_EMAIL
  retired:
    provider: github
    enabled: false
    settings:
      owner: acme
      repo: old
  broken:
    provider: jira
    environment:
      base_url: MISSING_BASE_URL
    secrets:
      token_env: JIRA_TOKEN
"""
_ENVIRONMENT = {
    "JIRA_BASE_URL": "https://acme.atlassian.net",
    "JIRA_EMAIL": "bot@acme.com",
    "CONFLUENCE_EMAIL": "bot@acme.com",
}


class _CountingConnections(FakeConnectionRepository):
    """Fake connection repository that counts writes."""

    writes: int = 0

    async def put(self, connection: Any, **kwargs: Any) -> Any:
        type(self).writes += 1
        return await super().put(connection, **kwargs)


@pytest.fixture
def stores() -> tuple[FakeProjectRepository, _CountingConnections]:
    _CountingConnections.writes = 0
    return FakeProjectRepository(), _CountingConnections()


def _service(stores: Any) -> ConnectionImportService:
    projects, connections = stores
    return ConnectionImportService(projects=projects, connections=connections, environ=_ENVIRONMENT)


@pytest.mark.asyncio
async def test_plan_lists_every_change_and_writes_nothing(stores: Any) -> None:
    plan = await _service(stores).plan(_LEGACY, tenant_id="DEFAULT")

    actions = {change.source_key: change.action for change in plan.changes}
    assert actions == {
        "jira-main": "create",
        "Wiki.Main": "create",
        "retired": "skip",
        "broken": "create",
    }
    broken = next(change for change in plan.changes if change.source_key == "broken")
    assert broken.reason is not None and "MISSING_BASE_URL" in broken.reason
    assert broken.connection is not None and broken.connection.status == "error"
    assert plan.projects_to_create == ("default", "wiki")
    assert _CountingConnections.writes == 0
    assert stores[0].projects == {}


@pytest.mark.asyncio
async def test_legacy_entries_keep_their_key_secrets_and_tuning(stores: Any) -> None:
    service = _service(stores)
    await service.apply(await service.plan(_LEGACY, tenant_id="DEFAULT"))
    connections = stores[1]

    jira = await connections.get_by_source_key(tenant_id="DEFAULT", source_key="jira-main")
    wiki = await connections.get_by_source_key(tenant_id="DEFAULT", source_key="Wiki.Main")

    assert jira is not None and wiki is not None
    assert (jira.project_id, jira.connection_id) == ("default", "jira-main")
    assert jira.credentials["api_token"]["secret_ref"] == "env:JIRA_TOKEN"
    assert jira.credentials["api_token"]["last4"] is None
    assert jira.credentials["email"] == "bot@acme.com"
    assert jira.config == {"project_keys": ["ENG"]}
    assert jira.tuning == {"requests_per_minute": 600}
    assert (wiki.project_id, wiki.connection_id) == ("wiki", "wiki-main")
    assert await stores[0].get("wiki", tenant_ids=None) is not None


@pytest.mark.asyncio
async def test_applying_the_example_file_twice_writes_once(
    stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    example = (REPO_ROOT / "config" / "connectors.example.yaml").read_text(encoding="utf-8")
    projects, connections = stores
    service = ConnectionImportService(
        projects=projects, connections=connections, environ={"LOCAL_SOURCE_PATH": str(source)}
    )

    first = await service.plan(example, tenant_id="DEFAULT", base_directory=REPO_ROOT / "config")
    await service.apply(first)
    rows = dict(connections.connections)
    second = await service.plan(example, tenant_id="DEFAULT", base_directory=REPO_ROOT / "config")
    result = await service.apply(second)

    assert first.summary() == {"create": 1}
    assert second.summary() == {"unchanged": 1}
    assert (result.created, result.updated) == (0, 0)
    assert connections.connections == rows
    assert _CountingConnections.writes == 1


@pytest.mark.asyncio
async def test_an_imported_entry_ingests_under_its_old_key(stores: Any) -> None:
    service = _service(stores)
    await service.apply(await service.plan(_LEGACY, tenant_id="DEFAULT"))
    resolved = await ConnectionResolver(stores[1], FakeConnectionRevisionRepository()).resolve(
        tenant_id="DEFAULT", source_key="jira-main"
    )

    prepared = prepare_source_submission(
        RuntimeSettings(),
        SourceSubmission(task_id="t", tenant_id="DEFAULT", connector_name="jira-main"),
        connection=resolved,
    )

    assert (prepared.connector_name, prepared.connection_id) == ("jira-main", "jira-main")


@pytest.mark.asyncio
async def test_export_round_trips_and_never_carries_a_value(stores: Any) -> None:
    service = _service(stores)
    await service.apply(await service.plan(_LEGACY, tenant_id="DEFAULT"))

    exported = await service.export(tenant_id="DEFAULT", project_id="default")
    replanned = await service.plan(exported, tenant_id="DEFAULT")

    assert "secret_ref: env:JIRA_TOKEN" in exported
    assert replanned.summary() == {"unchanged": 2}


@pytest.mark.asyncio
async def test_first_start_imports_once_and_never_reads_the_file_again(tmp_path: Path) -> None:
    path = tmp_path / "connectors.yaml"
    path.write_text(_LEGACY, encoding="utf-8")
    projects, connections = FakeProjectRepository(), FakeConnectionRepository()
    settings = FakeSettingsRepository(WorkspaceSettings(tenant_id="DEFAULT"))

    first = await import_connectors_once(
        path, projects=projects, connections=connections, settings=settings, environ=_ENVIRONMENT
    )
    path.write_text("not: [valid", encoding="utf-8")
    second = await import_connectors_once(
        path, projects=projects, connections=connections, settings=settings, environ=_ENVIRONMENT
    )

    assert first is not None and len(first.writes) == 3
    assert second is None
    marker = (await settings.get()).data[IMPORT_MARKER_KEY]
    assert (marker["created"], marker["skipped"], marker["errors"]) == (3, 1, 1)
    broken = await connections.get_by_source_key(tenant_id="DEFAULT", source_key="broken")
    assert broken is not None and broken.status == "error"
    assert broken.status_detail == {
        "errors": [
            {
                "field": "credentials.base_url",
                "issue": "environment variable MISSING_BASE_URL is not set",
            },
            {"field": "credentials.base_url", "issue": "required"},
            {"field": "credentials.email", "issue": "required"},
        ]
    }


@pytest.mark.asyncio
async def test_first_start_import_skips_installs_that_already_have_data(tmp_path: Path) -> None:
    path = tmp_path / "connectors.yaml"
    path.write_text(_LEGACY, encoding="utf-8")
    projects = FakeProjectRepository(
        {
            "a": Project(id="a", name="A", collection="a", tenant_id="t1"),
            "b": Project(id="b", name="B", collection="b", tenant_id="t2"),
        }
    )
    settings = FakeSettingsRepository(WorkspaceSettings(tenant_id="DEFAULT"))

    skipped = await import_connectors_once(
        path,
        projects=projects,
        connections=FakeConnectionRepository(),
        settings=settings,
        environ=_ENVIRONMENT,
    )
    missing = await import_connectors_once(
        tmp_path / "absent.yaml",
        projects=FakeProjectRepository(),
        connections=FakeConnectionRepository(),
        settings=settings,
    )

    assert skipped is None and missing is None
    assert IMPORT_MARKER_KEY not in (await settings.get()).data


@pytest.mark.asyncio
async def test_first_start_imports_next_to_rows_carried_over_from_api_v1_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connectors.yaml"
    path.write_text(_LEGACY, encoding="utf-8")
    projects = FakeProjectRepository(
        {"cpm": Project(id="cpm", name="CPM", collection="cpm", tenant_id="DEFAULT")}
    )
    connections = FakeConnectionRepository()
    migrated = (
        ConnectionBuilder.for_new(tenant_id="DEFAULT", project_id="cpm", connection_id="tickets")
        .source_type("jira")
        .build()
    )
    await connections.put(migrated)
    settings = FakeSettingsRepository(WorkspaceSettings(tenant_id="DEFAULT"))

    plan = await import_connectors_once(
        path, projects=projects, connections=connections, settings=settings, environ=_ENVIRONMENT
    )
    created = (
        ConnectionBuilder.for_new(tenant_id="DEFAULT", project_id="cpm", connection_id="tickets")
        .source_type("jira")
        .actor("u_huy")
        .build()
    )
    blocked = await import_connectors_once(
        path,
        projects=projects,
        connections=FakeConnectionRepository({created.id: created}),
        settings=FakeSettingsRepository(WorkspaceSettings(tenant_id="DEFAULT")),
        environ=_ENVIRONMENT,
    )

    assert plan is not None and len(plan.writes) == 3
    assert await connections.get_by_source_key(tenant_id="DEFAULT", source_key="jira-main")
    assert blocked is None
