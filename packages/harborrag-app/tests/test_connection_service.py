"""Connection use cases: put, test, sync and delete against in-memory stores."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app_test_connections import (
    CONNECTION_KEY,
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
    running_task,
)

from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionWrite,
)
from harborrag_app.workflow_control.control_plane.effect_recovery import (
    recover_pending_control_plane_effects,
)
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
    HarborImmutableFieldError,
    HarborNotFoundError,
    HarborPreconditionFailedError,
)
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.config.connectors.models import ConnectionValidationError
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])


@pytest.mark.asyncio
async def test_put_creates_a_connection_and_reads_secrets_back_as_status(
    world: ConnectionWorld,
) -> None:
    result = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")

    view = result.connection
    assert (result.created, result.version) == (True, 1)
    assert view["ref"] == "cpm/cpm-tickets" and view["name"] == "CPM tickets"
    assert view["credentials"]["api_token"] == {
        "set": True,
        "last4": "9f2c",
        "updated_at": view["credentials"]["api_token"]["updated_at"],
        "from": "stored",
    }
    assert view["credentials"]["base_url"] == "https://acme.atlassian.net"
    assert view["stats"]["documents"] == 7
    assert (view["created_by"], view["updated_by"]) == ("u_huy", "u_huy")
    assert "sentinel" not in repr(view)
    entries = await world.control_plane.activity.list(tenant_ids=None)
    assert entries[0].verb == "created" and "sentinel" not in entries[0].summary


@pytest.mark.asyncio
async def test_put_overwrites_keeping_rotating_and_clearing_secrets(world: ConnectionWorld) -> None:
    first = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u_huy")
    kept = await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite(
            "jira", {k: v for k, v in JIRA_WRITE.credentials.items() if k != "api_token"}, {}
        ),
        actor="u_ops",
        expected_version=first.version,
    )
    assert (kept.created, kept.version) == (False, 2)
    assert kept.connection["credentials"]["api_token"]["last4"] == "9f2c"
    assert kept.connection["config"] == {}
    assert kept.connection["name"] == "cpm-tickets"

    await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": "rotated-token-abcd"}, {}),
        actor="u_ops",
    )
    assert list(world.secrets.values.values()) == ["rotated-token-abcd"]

    with pytest.raises(ConnectionValidationError, match="does not match"):
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": None}, {}),
            actor="x",
        )


@pytest.mark.asyncio
async def test_put_preconditions_and_immutable_fields(world: ConnectionWorld) -> None:
    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", expected_version=1)
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", expected_version=9)
    with pytest.raises(HarborPreconditionFailedError):
        await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", create_only=True)
    with pytest.raises(HarborImmutableFieldError):
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("confluence", JIRA_WRITE.credentials, {"space_key": "ENG"}),
            actor="u",
        )


@pytest.mark.asyncio
async def test_invalid_writes_store_no_secret(world: ConnectionWorld) -> None:
    with pytest.raises(ConnectionValidationError) as unknown:
        await world.service.put(CONNECTION_KEY, ConnectionWrite("notion", {}, {}), actor="u")
    assert unknown.value.issues[0].field == "source_type"

    with pytest.raises(ConnectionValidationError) as invalid:
        await world.service.put(
            CONNECTION_KEY,
            ConnectionWrite("jira", {**JIRA_WRITE.credentials, "base_url": "nope"}, {}),
            actor="u",
        )
    assert invalid.value.issues[0].field == "credentials.base_url"

    with pytest.raises(HarborNotFoundError, match="project"):
        await world.service.put(ConnectionKey("acme", "missing", "x"), JIRA_WRITE, actor="u")
    assert world.secrets.values == {}


@pytest.mark.asyncio
async def test_a_rotated_secret_waits_for_the_running_ingestion(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.runs.active = running_task("ing_running", IngestionTaskState.RUNNING)

    await world.service.put(
        CONNECTION_KEY,
        ConnectionWrite("jira", {**JIRA_WRITE.credentials, "api_token": "rotated-token-abcd"}, {}),
        actor="u",
    )
    assert len(world.secrets.values) == 2

    await recover_pending_control_plane_effects(
        world.control_plane, connection_busy=world.service.connection_busy
    )
    assert len(world.secrets.values) == 2

    world.runs.active = None
    await recover_pending_control_plane_effects(
        world.control_plane, connection_busy=world.service.connection_busy
    )
    assert list(world.secrets.values.values()) == ["rotated-token-abcd"]


@pytest.mark.asyncio
async def test_sync_starts_one_ingestion_at_a_time(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    ingestion = await world.service.sync(CONNECTION_KEY, actor="u_huy", mode="full")

    assert ingestion["id"] == "ing_1" and ingestion["connection"] == "cpm/cpm-tickets"
    assert ingestion["status"] == "queued"
    assert ingestion["triggered_by"] == {"type": "user", "id": "u_huy"}
    command = world.submitter.commands[0]
    assert (command.connection_id, command.force_reprocess) == ("cpm/cpm-tickets", True)

    world.runs.active = world.runs.tasks["ing_1"]
    with pytest.raises(HarborAlreadyRunningError) as running:
        await world.service.sync(CONNECTION_KEY, actor="u")
    assert running.value.details == {"ingestion_id": "ing_1"}

    with pytest.raises(HarborCapabilityError):
        await world.service.sync(CONNECTION_KEY, actor="u", item_refs=("ENG-1",))


@pytest.mark.asyncio
async def test_put_with_sync_now_returns_the_ingestion(world: ConnectionWorld) -> None:
    result = await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u", sync_now=True)

    assert result.ingestion is not None and result.ingestion["id"] == "ing_1"


@pytest.mark.asyncio
async def test_delete_removes_documents_then_secrets_then_the_row(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.runs.active = running_task("ing_running", IngestionTaskState.RUNNING)
    with pytest.raises(HarborAlreadyRunningError):
        await world.service.delete(CONNECTION_KEY, actor="u")
    world.runs.active = None

    job_id = await world.service.delete(CONNECTION_KEY, actor="u", purge=True)
    with pytest.raises(ConnectionUnavailableError):
        await world.service.sync(CONNECTION_KEY, actor="u")
    await world.service.aclose()

    assert world.removal.calls == [
        {"tenant_id": "acme", "source_key": "cpm/cpm-tickets", "purge": True}
    ]
    assert world.secrets.values == {}
    with pytest.raises(HarborNotFoundError):
        await world.service.get(CONNECTION_KEY)
    job = await world.control_plane.jobs.get(job_id, tenant_ids=None)
    assert job is not None and job.status == "succeeded"


@pytest.mark.asyncio
async def test_a_failed_removal_keeps_the_row_deleting_and_resumes(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    world.removal.fail = True

    job_id = await world.service.delete(CONNECTION_KEY, actor="u")
    await world.service.aclose()

    job = await world.control_plane.jobs.get(job_id, tenant_ids=None)
    assert job is not None and (job.status, job.last_error) == ("failed", "RuntimeError")
    view, _ = await world.service.get(CONNECTION_KEY)
    assert view["status"] == "deleting"

    assert await world.service.resume_deletions() == 1
    with pytest.raises(HarborNotFoundError):
        await world.service.get(CONNECTION_KEY)


@pytest.mark.asyncio
async def test_testing_form_values_saves_nothing(world: ConnectionWorld, tmp_path: Path) -> None:
    missing = await world.service.test(
        CONNECTION_KEY,
        ConnectionWrite("local_file", {}, {"source_path": str(tmp_path / "missing")}),
    )
    assert missing["ok"] is False and missing["error_category"] == "unreachable"

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    healthy = await world.service.test(
        CONNECTION_KEY, ConnectionWrite("local_file", {}, {"source_path": str(tmp_path / "docs")})
    )
    assert healthy["ok"] is True
    assert [check["name"] for check in healthy["checks"]][0] == "schema"
    assert len(healthy["preview"]["sample"]) == 1

    with pytest.raises(ConnectionValidationError):
        await world.service.test(CONNECTION_KEY, ConnectionWrite("local_file", {}, {}))
    with pytest.raises(HarborNotFoundError):
        await world.service.test(CONNECTION_KEY, None)
    assert (await world.control_plane.connections.list(tenant_ids=None))[0] == []
    assert world.secrets.values == {}


@pytest.mark.asyncio
async def test_list_and_searchable_hide_nothing_but_secrets(world: ConnectionWorld) -> None:
    await world.service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    items, cursor = await world.service.list_page(tenant_id="acme")
    chips = await world.service.searchable(tenant_id="acme", project_id="cpm")

    assert [item["ref"] for item in items] == ["cpm/cpm-tickets"] and cursor is None
    assert chips == [
        {
            "ref": "cpm/cpm-tickets",
            "connection_id": "cpm-tickets",
            "source_type": "jira",
            "name": "CPM tickets",
            "documents": 7,
        }
    ]
    assert (await world.service.list_page(tenant_id="other"))[0] == []
    await asyncio.sleep(0)
