"""Per-connection reads and purges over the ingestion control tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, SourceIdentity

from .ingestion_control_fixtures import advance_to_verified, candidate, make_control_plane

_NOW = datetime(2026, 10, 29, 9, tzinfo=UTC)


def _identity(connection_id: str, scope: str, item: str) -> SourceIdentity:
    return SourceIdentity(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.CONFLUENCE,
        connection_id=connection_id,
        source_item_id=item,
        source_scope_id=scope,
    )


def _task(task_id: str, scope: str, status: IngestionTaskState, *, minutes: int) -> IngestionTask:
    return IngestionTask(
        task_id=task_id,
        source_scope_id=scope,
        status=status,
        request={"tenant_id": "DEFAULT"},
        submitted_at=_NOW + timedelta(minutes=minutes),
    )


async def _publish(
    control_plane: IngestionControlPlaneDatabase, connection_id: str, scope: str, item: str
) -> str:
    version = candidate(item, source=_identity(connection_id, scope, item))
    await advance_to_verified(control_plane, version)
    await control_plane.publisher.publish(
        document_id=str(version.document_id),
        candidate_document_version_id=str(version.document_version_id),
    )
    return str(version.document_id)


@pytest.mark.asyncio
async def test_connection_records_read_and_purge_only_their_connection(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        for connection_id, scope in (("wiki", "scope-wiki"), ("other", "scope-other")):
            await control_plane.source_scans.register_scope(
                source_scope_id=scope,
                connector_type="confluence",
                connection_id=connection_id,
                configuration_fingerprint="connection-1",
            )
        mine = await _publish(control_plane, "wiki", "scope-wiki", "page-1")
        theirs = await _publish(control_plane, "other", "scope-other", "page-2")
        tasks = control_plane.tasks
        await tasks.register(_task("old", "scope-wiki", IngestionTaskState.PENDING, minutes=0))
        await tasks.transition("old", IngestionTaskState.RUNNING)
        await tasks.transition("old", IngestionTaskState.COMPLETED, summary={"failed": 0})
        await tasks.register(_task("new", "scope-wiki", IngestionTaskState.PENDING, minutes=5))
        await tasks.register(
            _task("elsewhere", "scope-other", IngestionTaskState.PENDING, minutes=9)
        )
        records = control_plane.connection_records
        key = {"tenant_id": "DEFAULT", "connection_id": "wiki"}

        assert await records.active_document_ids(**key) == [mine]
        assert await records.document_count(**key) == 1
        assert await records.source_scope_ids(**key) == ["scope-wiki"]
        latest = await records.latest_task(**key)
        active = await records.active_task(**key)
        assert latest is not None and latest.task_id == "new"
        assert active is not None and active.task_id == "new"
        assert await records.active_task(tenant_id="OTHER", connection_id="wiki") is None

        counts = await records.purge(**key)

        assert (counts.documents, counts.source_scopes, counts.tasks) == (1, 1, 2)
        assert await records.latest_task(**key) is None
        assert await records.active_document_ids(**key) == []
        assert await records.active_document_ids(tenant_id="DEFAULT", connection_id="other") == [
            theirs
        ]
        assert await tasks.get("elsewhere") is not None
