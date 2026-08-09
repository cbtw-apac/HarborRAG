from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    CleanupJobState,
    DiscoveredSourceItem,
    DocumentFailure,
    DocumentVersionState,
    FailureCategory,
    IngestionTask,
    IngestionTaskState,
    ProjectionManifest,
    SourceAdmissionDecision,
    TaskDocumentResult,
)

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)


@pytest.mark.asyncio
async def test_retried_task_finalization_recovers_a_stage_failure(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        task = IngestionTask(
            task_id="task-retried-finalization",
            source_scope_id="scope-engineering",
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence"},
        )
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        await control_plane.tasks.transition(
            task.task_id,
            IngestionTaskState.FAILED,
            summary={"failed_stage": "removal_reconciliation"},
        )

        expected_summary = {
            "discovered": 2,
            "published": 0,
            "unchanged": 2,
            "failed": 0,
        }
        await control_plane.tasks.finalize(
            task.task_id,
            IngestionTaskState.COMPLETED,
            summary=expected_summary,
        )
        await control_plane.tasks.finalize(
            task.task_id,
            IngestionTaskState.COMPLETED,
            summary=expected_summary,
        )

        stored = await control_plane.tasks.get(task.task_id)
        assert stored is not None
        assert stored.status == IngestionTaskState.COMPLETED
        assert stored.summary == expected_summary
        assert stored.started_at is not None
        assert stored.completed_at is not None


@pytest.mark.asyncio
async def test_partial_task_finalization_is_terminal_and_idempotent(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        task = IngestionTask(
            task_id="task-partial",
            source_scope_id="scope-engineering",
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence"},
        )
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        summary = {"published": 2, "unchanged": 1, "failed": 1}

        await control_plane.tasks.finalize(
            task.task_id,
            IngestionTaskState.PARTIAL,
            summary=summary,
        )
        await control_plane.tasks.finalize(
            task.task_id,
            IngestionTaskState.PARTIAL,
            summary=summary,
        )

        stored = await control_plane.tasks.get(task.task_id)
        assert stored is not None
        assert stored.status == IngestionTaskState.PARTIAL
        assert stored.summary == summary
        with pytest.raises(HarborConflictError, match="cannot change outcome"):
            await control_plane.tasks.finalize(
                task.task_id,
                IngestionTaskState.COMPLETED,
                summary=summary,
            )


@pytest.mark.asyncio
async def test_scan_registration_preserves_the_previous_admission_view(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        value = candidate("first")
        first_scan = await scans.start("scope-engineering")
        first = await scans.record_seen(
            scan_id=first_scan,
            item=DiscoveredSourceItem(
                source_identity=value.source_identity,
                document_id=value.document_id,
                source_version="1",
                admission_change_key="admission-1",
                descriptor={"title": "Old"},
            ),
        )
        await scans.complete(first_scan)
        second_scan = await scans.start("scope-engineering")
        metadata_change = await scans.record_seen(
            scan_id=second_scan,
            item=DiscoveredSourceItem(
                source_identity=value.source_identity,
                document_id=value.document_id,
                source_version="1",
                admission_change_key="admission-1",
                descriptor={"title": "New"},
            ),
        )

        assert first.decision == SourceAdmissionDecision.NEW
        assert metadata_change.decision == SourceAdmissionDecision.METADATA_CHANGED
        assert metadata_change.previous_descriptor == {"title": "Old"}


@pytest.mark.asyncio
async def test_active_snapshot_exposes_fingerprints_for_admission_reuse(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("published")
        await advance_to_verified(control_plane, value)
        await control_plane.publisher.publish(
            document_id=str(value.document_id),
            candidate_document_version_id=str(value.document_version_id),
        )

        snapshot = await control_plane.document_versions.active_snapshot(str(value.document_id))

        assert snapshot is not None
        assert snapshot.state == DocumentVersionState.ACTIVE
        assert snapshot.fingerprints == value.fingerprints


@pytest.mark.asyncio
async def test_projection_manifest_and_version_inputs_are_immutable(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("immutable")
        await advance_to_verified(control_plane, value)

        with pytest.raises(HarborConflictError, match="immutable"):
            await control_plane.document_versions.save_projection_manifest(
                ProjectionManifest(
                    document_id=value.document_id,
                    document_version_id=value.document_version_id,
                    route_point_ids=("different-route",),
                )
            )


@pytest.mark.asyncio
async def test_failures_cleanup_and_task_results_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("failed projection")
        await control_plane.document_versions.create_candidate(value)
        failure = DocumentFailure(
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            failed_stage="WriteGraphProjection",
            category=FailureCategory.GRAPH_WRITE_FAILURE,
            retryable=True,
            safe_error_code="graph_unavailable",
        )
        await control_plane.reliability.record_failure(failure)
        await control_plane.reliability.record_failure(failure)
        first = await control_plane.reliability.enqueue_cleanup(
            document_id=str(value.document_id),
            document_version_id=str(value.document_version_id),
        )
        replay = await control_plane.reliability.enqueue_cleanup(
            document_id=str(value.document_id),
            document_version_id=str(value.document_version_id),
        )

        assert replay == first
        assert (await control_plane.reliability.pending_cleanup_jobs()) == (first,)
        await control_plane.reliability.start_cleanup(first.cleanup_job_id)
        await control_plane.reliability.fail_cleanup(
            first.cleanup_job_id,
            safe_error_code="qdrant_unavailable",
        )
        failed = (await control_plane.reliability.pending_cleanup_jobs())[0]
        assert failed.status == CleanupJobState.FAILED
        assert failed.attempt_count == 1

        task = IngestionTask(
            task_id="task-1",
            source_scope_id="scope-engineering",
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence"},
        )
        await control_plane.tasks.create(task)
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        result = TaskDocumentResult(
            task_id=task.task_id,
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            status="FAILED",
            result={"safe_error_code": "graph_unavailable"},
        )
        await asyncio.gather(
            *(control_plane.tasks.record_document_result(result) for _ in range(8))
        )
        assert await control_plane.tasks.document_results(task.task_id) == (result,)
        await control_plane.tasks.transition(
            task.task_id,
            IngestionTaskState.FAILED,
            summary={"failed": 1},
        )


@pytest.mark.asyncio
async def test_idempotency_keys_are_scoped_to_tenant(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        first = IngestionTask(
            task_id="task-tenant-a",
            source_scope_id="scope-a",
            status=IngestionTaskState.PENDING,
            request={"tenant_id": "tenant-a"},
        )
        second = IngestionTask(
            task_id="task-tenant-b",
            source_scope_id="scope-b",
            status=IngestionTaskState.PENDING,
            request={"tenant_id": "tenant-b"},
        )

        first_registration = await control_plane.tasks.register(
            first,
            idempotency_key="daily-sync",
            request_hash="a" * 64,
        )
        second_registration = await control_plane.tasks.register(
            second,
            idempotency_key="daily-sync",
            request_hash="b" * 64,
        )

        assert first_registration.created is True
        assert second_registration.created is True


@pytest.mark.asyncio
async def test_pending_cleanup_selection_is_scope_isolated(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        cleanup_by_scope = {}
        for scope_id, source_item_id in (
            ("scope-a", "page-a"),
            ("scope-b", "page-b"),
        ):
            await control_plane.source_scans.register_scope(
                source_scope_id=scope_id,
                connector_type="confluence",
                connection_id="wiki.example",
                configuration_fingerprint=f"{scope_id}-config",
            )
            source = source_identity(source_item_id).model_copy(
                update={"source_scope_id": scope_id}
            )
            value = candidate(scope_id, source=source)
            await control_plane.document_versions.create_candidate(value)
            scan_id = await control_plane.source_scans.start(scope_id)
            await control_plane.source_scans.record_seen(
                scan_id=scan_id,
                item=DiscoveredSourceItem(
                    source_identity=source,
                    document_id=value.document_id,
                    source_version="1",
                    admission_change_key=value.fingerprints.admission_change_key,
                ),
            )
            await control_plane.source_scans.complete(scan_id)
            cleanup_by_scope[scope_id] = await control_plane.reliability.enqueue_cleanup(
                document_id=str(value.document_id),
                document_version_id=str(value.document_version_id),
            )

        selected = await control_plane.reliability.pending_cleanup_jobs(source_scope_id="scope-a")
        assert selected == (cleanup_by_scope["scope-a"],)
        with pytest.raises(ValueError, match="not both"):
            await control_plane.reliability.pending_cleanup_jobs(
                source_scope_id="scope-a",
                document_ids=(str(cleanup_by_scope["scope-b"].document_id),),
            )
