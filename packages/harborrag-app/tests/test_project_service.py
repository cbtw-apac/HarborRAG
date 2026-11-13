"""Project use cases: put with preconditions, paging and cascading deletes."""

from __future__ import annotations

from typing import Any

import pytest
from app_test_connections import JIRA_WRITE, Removal, Runs, Submitter, fake_control_plane

from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionService,
)
from harborrag_app.workflow_control.control_plane.projects import (
    ProjectIdTakenError,
    ProjectService,
)
from harborrag_core.contracts.errors import (
    HarborInUseError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
)
from harborrag_core.domain.project import Project
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.models import ConnectionValidationError


@pytest.fixture
def services() -> tuple[ProjectService, ConnectionService, ControlPlaneRepositories, Removal]:
    runs = Runs()
    removal = Removal()
    control_plane = fake_control_plane(
        [Project(id="theirs", name="T", collection="theirs", tenant_id="other")], FakeSecrets()
    )

    async def provide_runs() -> Runs:
        return runs

    connections = ConnectionService(
        control_plane=lambda: control_plane,
        runs=provide_runs,
        ingestions=Submitter(runs),
        removal=lambda: removal,  # type: ignore[arg-type,return-value]
        instance_id="test",
    )
    projects = ProjectService(
        control_plane=lambda: control_plane, runs=provide_runs, connections=connections
    )
    return projects, connections, control_plane, removal


@pytest.mark.asyncio
async def test_put_creates_then_updates_under_preconditions(services: Any) -> None:
    projects, *_ = services

    view, created, version = await projects.put(
        tenant_id="acme", project_id="cpm", name="CPM", description=None, actor="u"
    )
    assert (created, version) == (True, 1)
    assert view == {
        "project_id": "cpm",
        "tenant_id": "acme",
        "project_name": "CPM",
        "project_description": None,
        "connection_count": 0,
        "document_count": 0,
        "created_at": view["created_at"],
        "updated_at": view["updated_at"],
    }

    _, created, version = await projects.put(
        tenant_id="acme",
        project_id="cpm",
        name="CPM candidates",
        description="Tickets",
        actor="u",
        expected_version=1,
    )
    assert (created, version) == (False, 2)
    with pytest.raises(HarborPreconditionFailedError):
        await projects.put(
            tenant_id="acme",
            project_id="cpm",
            name="x",
            description=None,
            actor="u",
            expected_version=1,
        )
    with pytest.raises(HarborPreconditionFailedError):
        await projects.put(
            tenant_id="acme",
            project_id="cpm",
            name="x",
            description=None,
            actor="u",
            create_only=True,
        )
    with pytest.raises(ProjectIdTakenError):
        await projects.put(
            tenant_id="acme", project_id="theirs", name="x", description=None, actor="u"
        )
    with pytest.raises(ConnectionValidationError):
        await projects.put(
            tenant_id="acme", project_id="Bad Id", name="x", description=None, actor="u"
        )


@pytest.mark.asyncio
async def test_put_with_an_unchanged_body_keeps_version_and_activity(services: Any) -> None:
    projects, _, control_plane, _ = services
    await projects.put(
        tenant_id="acme", project_id="qc1347-bug", name="Bug repro", description=None, actor="u"
    )
    activity_before = len(control_plane.activity.entries)

    for _ in range(2):
        view, created, version = await projects.put(
            tenant_id="acme",
            project_id="qc1347-bug",
            name="Bug repro",
            description=None,
            actor="u",
        )
        assert (created, version) == (False, 1)

    assert view["project_name"] == "Bug repro"
    assert len(control_plane.activity.entries) == activity_before
    stored = await control_plane.projects.get("qc1347-bug", tenant_ids=None)
    assert stored is not None
    assert stored.version == 1

    with pytest.raises(HarborPreconditionFailedError):
        await projects.put(
            tenant_id="acme",
            project_id="qc1347-bug",
            name="Bug repro",
            description=None,
            actor="u",
            expected_version=0,
        )


@pytest.mark.asyncio
async def test_list_pages_by_id_and_filters_by_name(services: Any) -> None:
    projects, *_ = services
    for project_id, name in (("b", "Beta"), ("a", "Alpha"), ("c", "Gamma")):
        await projects.put(
            tenant_id="acme", project_id=project_id, name=name, description=None, actor="u"
        )

    first, cursor = await projects.list_page(tenant_id="acme", limit=2)
    second, last = await projects.list_page(tenant_id="acme", limit=2, cursor=cursor)
    filtered, _ = await projects.list_page(tenant_id="acme", query="alp")

    assert [item["project_id"] for item in first + second] == ["a", "b", "c"]
    assert last is None
    assert [item["project_id"] for item in filtered] == ["a"]


@pytest.mark.asyncio
async def test_delete_needs_cascade_once_connections_exist(services: Any) -> None:
    projects, connections, control_plane, removal = services
    await projects.put(tenant_id="acme", project_id="cpm", name="CPM", description=None, actor="u")
    assert await projects.delete(tenant_id="acme", project_id="cpm", actor="u") is None
    await projects.put(tenant_id="acme", project_id="cpm", name="CPM", description=None, actor="u")
    await connections.put(ConnectionKey("acme", "cpm", "cpm-tickets"), JIRA_WRITE, actor="u")

    with pytest.raises(HarborInUseError) as in_use:
        await projects.delete(tenant_id="acme", project_id="cpm", actor="u")
    assert in_use.value.details == {"connection_refs": ["cpm/cpm-tickets"]}

    job_id = await projects.delete(tenant_id="acme", project_id="cpm", actor="u", cascade=True)
    await connections.aclose()

    assert job_id is not None
    assert removal.calls[0]["purge"] is True
    with pytest.raises(HarborNotFoundError):
        await projects.get(tenant_id="acme", project_id="cpm")
    job = await control_plane.jobs.get(job_id, tenant_ids=None)
    assert job is not None and job.status == "succeeded"


@pytest.mark.asyncio
async def test_a_cascade_that_hits_a_failure_finishes_on_resume(services: Any) -> None:
    projects, connections, control_plane, removal = services
    await projects.put(tenant_id="acme", project_id="cpm", name="CPM", description=None, actor="u")
    for connection_id in ("cpm-tickets", "cpm-wiki"):
        await connections.put(ConnectionKey("acme", "cpm", connection_id), JIRA_WRITE, actor="u")
    removal.fail = True

    job_id = await projects.delete(tenant_id="acme", project_id="cpm", actor="u", cascade=True)
    await connections.aclose()
    assert job_id is not None
    interrupted = await control_plane.jobs.get(job_id, tenant_ids=None)

    await connections.resume_deletions()
    after_connections = await control_plane.jobs.get(job_id, tenant_ids=None)
    still_there = await control_plane.projects.get("cpm", tenant_ids=None)
    await projects.resume_deletions()
    finished = await control_plane.jobs.get(job_id, tenant_ids=None)

    assert interrupted is not None and interrupted.status == "running"
    assert interrupted.last_error is not None
    assert after_connections is not None and after_connections.status == "running"
    assert still_there is not None
    assert finished is not None and finished.status == "succeeded"
    assert await control_plane.projects.get("cpm", tenant_ids=None) is None
