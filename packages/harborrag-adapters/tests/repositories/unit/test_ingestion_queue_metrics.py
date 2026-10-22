"""Queue counts and the publication anchor behind the worker's live ingestion gauges."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.summary_schema import (
    SUMMARY_SCOPES,
)
from harborrag_core.ingestion import (
    DocumentVersionState,
    IngestionTask,
    IngestionTaskState,
    TaskDocumentResult,
)
from harborrag_core.schemas.ids import DocumentId

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .topology_fixtures import permit


async def _running_task(
    control: Any,
    task_id: str,
    *,
    admitted: int | None,
    finished: int = 0,
    source_scope_id: str = "scope-engineering",
) -> None:
    await control.tasks.create(
        IngestionTask(
            task_id=task_id,
            source_scope_id=source_scope_id,
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence"},
        )
    )
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    if admitted is not None:
        await control.tasks.update_summary(task_id, {"admitted": admitted})
    for index in range(finished):
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=DocumentId(f"{task_id}-doc-{index}"),
                status="published",
                result={},
            )
        )


@pytest.mark.asyncio
async def test_pending_counts_admitted_documents_without_an_outcome(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.source_scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config",
        )
        # 5 admitted, 2 finished: 3 still queued or in flight.
        await _running_task(control, "task-running", admitted=5, finished=2)
        # Discovery has not admitted anything yet, so nothing is pending.
        await _running_task(control, "task-discovering", admitted=None)
        # Every admitted document has an outcome.
        await _running_task(control, "task-drained", admitted=2, finished=2)
        # Finished tasks never count, whatever their summary says.
        await _running_task(control, "task-done", admitted=4)
        await control.tasks.transition("task-done", IngestionTaskState.COMPLETED)
        # A scope the catalog does not know yet still reports its backlog.
        await _running_task(
            control, "task-unscoped", admitted=1, source_scope_id="scope-unregistered"
        )

        counts = await control.queue_metrics.counts()
        running = await control.tasks.get("task-running")

    assert counts.pending_by_connector == {"confluence": 3, "unknown": 1}
    assert running is not None and running.started_at is not None
    assert counts.oldest_pending_at is not None
    assert counts.oldest_pending_at.tzinfo is not None
    assert counts.oldest_pending_at <= running.started_at


@pytest.mark.asyncio
async def test_queue_counts_split_in_progress_and_enrichment(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        pending = candidate("pending", source=source_identity("page-pending"))
        await control.document_versions.create_candidate(pending)
        captured = candidate("captured", source=source_identity("page-captured"))
        await control.document_versions.create_candidate(captured)
        await control.document_versions.transition(
            str(captured.document_version_id), DocumentVersionState.RAW_CAPTURED
        )
        async with control._client.sessions.begin() as session:
            await session.execute(
                SUMMARY_SCOPES.insert().values(
                    tenant_id="DEFAULT",
                    source_scope_id="scope-engineering",
                    revision=1,
                    fence=0,
                    policy={"enabled": True},
                    execution="queued",
                )
            )

        counts = await control.queue_metrics.counts()

    # A PENDING version row is not backlog: it exists only after parsing.
    assert counts.pending_by_connector == {}
    assert counts.oldest_pending_at is None
    assert counts.in_progress_by_state == {"RAW_CAPTURED": 1}
    assert counts.enrichment_pending == 1


@pytest.mark.asyncio
async def test_queue_counts_are_empty_without_work(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        counts = await control.queue_metrics.counts()

    assert counts.pending_by_connector == {}
    assert counts.oldest_pending_at is None
    assert counts.in_progress_by_state == {}
    assert counts.enrichment_pending == 0


@pytest.mark.asyncio
async def test_pending_published_at_is_the_earliest_publication_since_dirty(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        version = candidate("one")
        await permit(control, str(version.document_id))
        await advance_to_verified(control, version)
        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        lease = cast(Any, SimpleNamespace(tenant_id="DEFAULT", source_scope_id="scope-engineering"))

        published_at = await control.summaries.pending_published_at(lease)

        async with control._client.sessions.begin() as session:
            await session.execute(
                update(SUMMARY_SCOPES)
                .where(SUMMARY_SCOPES.c.source_scope_id == "scope-engineering")
                .values(dirty_since=None)
            )
        cleared = await control.summaries.pending_published_at(lease)
        tenant = await control.summaries.pending_published_at(
            cast(Any, SimpleNamespace(tenant_id="DEFAULT", source_scope_id="@tenant"))
        )

    assert published_at is not None and published_at.tzinfo is not None
    assert cleared is None
    assert tenant is None
