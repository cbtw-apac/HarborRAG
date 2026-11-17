"""The run listing narrows to connections and to a submission time."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState

from .ingestion_control_fixtures import make_control_plane


async def _seed(
    control_plane: IngestionControlPlaneDatabase, task_id: str, source_key: str
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
            request={"tenant_id": "acme", "connection_id": source_key},
        )
    )


@pytest.mark.asyncio
async def test_listing_keeps_only_the_runs_of_the_given_connections(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-a", "cpm/tickets")
        await _seed(control_plane, "run-b", "cpm/wiki")

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}), scope_connection_ids=frozenset({"cpm/tickets"})
        )
        both = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}),
            scope_connection_ids=frozenset({"cpm/tickets", "cpm/wiki"}),
        )

        assert [task.task_id for task in page.items] == ["run-a"]
        assert {task.task_id for task in both.items} == {"run-a", "run-b"}


@pytest.mark.asyncio
async def test_an_empty_connection_set_matches_no_run(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-a", "cpm/tickets")

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}), scope_connection_ids=frozenset()
        )

        assert page.items == ()
        assert page.has_more is False


@pytest.mark.asyncio
async def test_created_after_cuts_older_runs(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control_plane:
        await _seed(control_plane, "run-old", "cpm/tickets")
        await asyncio.sleep(0.01)
        await _seed(control_plane, "run-new", "cpm/tickets")
        old = await control_plane.tasks.get("run-old")
        assert old is not None
        assert old.submitted_at is not None

        page = await control_plane.tasks.list_tasks(
            tenant_ids=frozenset({"acme"}),
            created_after=old.submitted_at + timedelta(microseconds=1),
        )

        assert [task.task_id for task in page.items] == ["run-new"]
