"""The run listing narrows to connections, to who started a run and to a submission time."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskTrigger

from .ingestion_control_fixtures import make_control_plane

pytestmark = pytest.mark.unit


async def _seed(
    control_plane: IngestionControlPlaneDatabase,
    task_id: str,
    source_key: str,
    started_by: Mapping[str, str] | None = None,
) -> None:
    scope = f"scope-{source_key}"
    await control_plane.source_scans.register_scope(
        tenant_id="acme",
        source_scope_id=scope,
        connector_type="jira",
        connection_id=source_key,
        configuration_fingerprint="f" * 64,
    )
    await control_plane.tasks.create(
        IngestionTask(
            task_id=task_id,
            source_scope_id=scope,
            status=IngestionTaskState.PENDING,
            request={"tenant_id": "acme", "connection_id": source_key, **(started_by or {})},
        )
    )


async def _listed(control_plane: IngestionControlPlaneDatabase, trigger: TaskTrigger) -> set[str]:
    page = await control_plane.tasks.list_tasks(tenant_ids=frozenset({"acme"}), trigger=trigger)
    return {task.task_id for task in page.items}


@pytest.mark.asyncio
async def test_listing_keeps_the_runs_the_trigger_started(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-user", "demo/tickets", {"actor": "u", "actor_type": "user"})
        await _seed(control_plane, "run-unrecorded", "demo/tickets")
        await _seed(
            control_plane, "run-key", "demo/tickets", {"actor": "k", "actor_type": "api_key"}
        )
        await _seed(
            control_plane, "run-hourly", "demo/wiki", {"actor": "hourly", "actor_type": "schedule"}
        )
        await _seed(
            control_plane, "run-daily", "demo/wiki", {"actor": "daily", "actor_type": "schedule"}
        )

        user = await _listed(control_plane, TaskTrigger("user"))
        api_key = await _listed(control_plane, TaskTrigger("api_key"))
        schedule = await _listed(control_plane, TaskTrigger("schedule"))
        hourly = await _listed(control_plane, TaskTrigger("schedule", actor="hourly"))
        nobody = await _listed(control_plane, TaskTrigger("schedule", actor="weekly"))

    assert user == {"run-user", "run-unrecorded"}
    assert api_key == {"run-key"}
    assert schedule == {"run-hourly", "run-daily"}
    assert hourly == {"run-hourly"}
    assert nobody == set()


@pytest.mark.asyncio
async def test_source_keys_names_each_connection_of_the_tenant_once(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-a", "demo/tickets")
        await _seed(control_plane, "run-b", "demo/tickets")
        await _seed(control_plane, "run-c", "legacy-wiki")
        await control_plane.source_scans.register_scope(
            tenant_id="globex",
            source_scope_id="scope-globex",
            connector_type="jira",
            connection_id="ops/tickets",
            configuration_fingerprint="f" * 64,
        )

        keys = await control_plane.connection_records.source_keys(tenant_id="acme")

    assert keys == frozenset({"demo/tickets", "legacy-wiki"})


@pytest.mark.asyncio
async def test_listing_keeps_only_the_runs_of_the_given_connections(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-a", "demo/tickets")
        await _seed(control_plane, "run-b", "demo/wiki")

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}), scope_connection_ids=frozenset({"demo/tickets"})
        )
        both = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}),
            scope_connection_ids=frozenset({"demo/tickets", "demo/wiki"}),
        )

        assert [task.task_id for task in page.items] == ["run-a"]
        assert {task.task_id for task in both.items} == {"run-a", "run-b"}


@pytest.mark.asyncio
async def test_an_empty_connection_set_matches_no_run(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-a", "demo/tickets")

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}), scope_connection_ids=frozenset()
        )

        assert page.items == ()
        assert page.has_more is False


@pytest.mark.asyncio
async def test_created_after_cuts_older_runs(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-old", "demo/tickets")
        await asyncio.sleep(0.01)
        await _seed(control_plane, "run-new", "demo/tickets")
        old = await control_plane.tasks.get("run-old")
        assert old is not None
        assert old.submitted_at is not None

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}),
            created_after=old.submitted_at + timedelta(microseconds=1),
        )

        assert [task.task_id for task in page.items] == ["run-new"]
