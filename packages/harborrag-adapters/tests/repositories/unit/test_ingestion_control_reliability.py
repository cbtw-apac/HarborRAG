from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control.task_lifecycle import (
    SUBMISSION_CLAIM_STALE_AFTER,
)
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
async def test_paused_task_can_transition_to_failed_and_persists_status(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        task = IngestionTask(
            task_id="task-paused-failure",
            source_scope_id="scope-engineering",
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence"},
        )
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.PAUSED)
        await control_plane.tasks.transition(
            task.task_id,
            IngestionTaskState.FAILED,
            summary={"failed_stage": "document_dispatch", "error_code": "child_timeout"},
        )

        stored = await control_plane.tasks.get(task.task_id)
        assert stored is not None
        assert stored.status == IngestionTaskState.FAILED
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


@pytest.mark.asyncio
async def test_pending_submissions_skip_executor_and_submitted(tmp_path: Path) -> None:
    # Only a durable row whose Temporal start is unconfirmed belongs to the outbox:
    # inline (executor) rows run in-process and submitted rows already started.
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        markers: tuple[tuple[str, dict[str, object]], ...] = (
            ("task-unmarked", {}),
            ("task-submitted", {"submission_state": "submitted"}),
            ("task-executor", {"submission_state": "executor"}),
        )
        for task_id, summary in markers:
            await control_plane.tasks.create(
                IngestionTask(
                    task_id=task_id,
                    source_scope_id="scope-engineering",
                    status=IngestionTaskState.PENDING,
                    request={"connector": "confluence", "task": task_id},
                    summary=summary,
                )
            )

        pending = await control_plane.tasks.pending_submissions()

        assert [task.task_id for task in pending] == ["task-unmarked"]


@pytest.mark.asyncio
async def test_fail_if_unsubmitted_only_fails_unsubmitted_pending_tasks(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    failure = {"failed_stage": "submission", "error_code": "submission_failed"}
    async with control_plane:
        markers: tuple[tuple[str, dict[str, object]], ...] = (
            ("task-unsubmitted", {}),
            ("task-submitted", {"submission_state": "submitted"}),
            ("task-executor", {"submission_state": "executor"}),
            ("task-running", {}),
        )
        for task_id, summary in markers:
            await control_plane.tasks.create(
                IngestionTask(
                    task_id=task_id,
                    source_scope_id="scope-engineering",
                    status=IngestionTaskState.PENDING,
                    request={"connector": "confluence", "task": task_id},
                    summary=summary,
                )
            )
        await control_plane.tasks.transition("task-running", IngestionTaskState.RUNNING)
        await control_plane.tasks.update_summary("task-running", {"stage": "DISCOVERING"})

        outcomes = {
            task_id: await control_plane.tasks.fail_if_unsubmitted(task_id, summary=failure)
            for task_id, _ in markers
        }
        missing = await control_plane.tasks.fail_if_unsubmitted("task-missing", summary=failure)

        assert outcomes == {
            "task-unsubmitted": IngestionTaskState.FAILED,
            "task-submitted": IngestionTaskState.PENDING,
            "task-executor": IngestionTaskState.PENDING,
            "task-running": IngestionTaskState.RUNNING,
        }
        assert missing is None
        failed = await control_plane.tasks.get("task-unsubmitted")
        assert failed is not None
        assert failed.summary == failure
        assert failed.completed_at is not None
        running = await control_plane.tasks.get("task-running")
        assert running is not None
        assert running.summary == {"stage": "DISCOVERING"}
        submitted = await control_plane.tasks.get("task-submitted")
        assert submitted is not None
        assert submitted.summary == {"submission_state": "submitted"}


async def _pending(control_plane, task_id: str, summary: dict[str, object]) -> None:
    await control_plane.tasks.create(
        IngestionTask(
            task_id=task_id,
            source_scope_id="scope-engineering",
            status=IngestionTaskState.PENDING,
            request={"connector": "confluence", "task": task_id},
            summary=summary,
        )
    )


def _claimed_at(age: timedelta) -> str:
    return (datetime.now(UTC) - age).isoformat(timespec="microseconds")


@pytest.mark.asyncio
async def test_a_claimed_submission_is_not_failed_by_fail_if_unsubmitted(
    tmp_path: Path,
) -> None:
    # Recovery claims the row before it starts the workflow, so a connect failure on
    # the original submit path can no longer fail a run that is about to start.
    control_plane = make_control_plane(tmp_path)
    failure = {"failed_stage": "submission", "error_code": "submission_failed"}
    async with control_plane:
        await _pending(control_plane, "task-a", {})

        assert await control_plane.tasks.claim_submission("task-a") is not None
        outcome = await control_plane.tasks.fail_if_unsubmitted("task-a", summary=failure)

        assert outcome is IngestionTaskState.PENDING
        claimed = await control_plane.tasks.get("task-a")
        assert claimed is not None
        assert claimed.status is IngestionTaskState.PENDING
        assert claimed.summary["submission_state"] == "submitting"
        claimed_at = datetime.fromisoformat(str(claimed.summary["submission_claimed_at"]))
        assert claimed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_claim_submission_is_exclusive_until_the_claim_goes_stale(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        await _pending(control_plane, "task-fresh", {})
        await _pending(
            control_plane,
            "task-stale",
            {
                "submission_state": "submitting",
                "submission_claimed_at": _claimed_at(
                    SUBMISSION_CLAIM_STALE_AFTER + timedelta(minutes=1)
                ),
            },
        )
        await _pending(control_plane, "task-submitted", {"submission_state": "submitted"})
        await _pending(control_plane, "task-executor", {"submission_state": "executor"})
        await _pending(control_plane, "task-running", {})
        await control_plane.tasks.transition("task-running", IngestionTaskState.RUNNING)

        token = await control_plane.tasks.claim_submission("task-fresh")
        assert isinstance(token, str)
        claimed = await control_plane.tasks.get("task-fresh")
        assert claimed is not None and claimed.summary["submission_claimed_at"] == token
        assert await control_plane.tasks.claim_submission("task-fresh") is None
        assert await control_plane.tasks.claim_submission("task-stale") is not None
        assert await control_plane.tasks.claim_submission("task-submitted") is None
        assert await control_plane.tasks.claim_submission("task-executor") is None
        assert await control_plane.tasks.claim_submission("task-running") is None
        assert await control_plane.tasks.claim_submission("task-missing") is None


@pytest.mark.asyncio
async def test_pending_submissions_skip_fresh_claims_but_return_stale_ones(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        await _pending(control_plane, "task-unclaimed", {})
        await _pending(
            control_plane,
            "task-fresh",
            {
                "submission_state": "submitting",
                "submission_claimed_at": _claimed_at(timedelta(seconds=5)),
            },
        )
        await _pending(
            control_plane,
            "task-stale",
            {
                "submission_state": "submitting",
                "submission_claimed_at": _claimed_at(
                    SUBMISSION_CLAIM_STALE_AFTER + timedelta(seconds=1)
                ),
            },
        )
        # A claim without a timestamp cannot age out by comparison; treat it as stale
        # rather than stranding the task.
        await _pending(control_plane, "task-undated", {"submission_state": "submitting"})

        pending = await control_plane.tasks.pending_submissions()

        assert sorted(task.task_id for task in pending) == [
            "task-stale",
            "task-unclaimed",
            "task-undated",
        ]


@pytest.mark.asyncio
async def test_releasing_a_claim_only_clears_an_unconfirmed_claim(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        await _pending(control_plane, "task-a", {"discovered": 0})
        await _pending(control_plane, "task-b", {"submission_state": "submitted"})

        token = await control_plane.tasks.claim_submission("task-a")
        assert token is not None
        # Only the claim's owner may release it: a stale token is someone else's.
        assert await control_plane.tasks.release_submission_claim("task-a", "stale") is False
        assert await control_plane.tasks.release_submission_claim("task-a", token) is True
        # The other submit path may have confirmed the start meanwhile; keep that.
        assert await control_plane.tasks.release_submission_claim("task-b", token) is False
        assert await control_plane.tasks.release_submission_claim("task-missing", token) is False

        released = await control_plane.tasks.get("task-a")
        confirmed = await control_plane.tasks.get("task-b")
        assert released is not None and confirmed is not None
        assert released.summary == {"discovered": 0}
        assert confirmed.summary == {"submission_state": "submitted"}
        assert [task.task_id for task in await control_plane.tasks.pending_submissions()] == [
            "task-a"
        ]


def _commit_before_the_update(control_plane, database: Path, task_id: str, summary: dict):
    """Commit ``summary`` from another connection just before the next task UPDATE.

    That is the window between a read and its write: a decision taken on the read
    must not survive the concurrent change. sqlite ignores FOR UPDATE, so only the
    UPDATE's own predicate can notice it.
    """

    import json
    import sqlite3

    from sqlalchemy import event

    engine = control_plane._client.raw.sync_engine
    fired: list[str] = []

    def before_cursor_execute(*event_args: object) -> None:
        # (conn, cursor, statement, parameters, context, executemany)
        statement = str(event_args[2])
        if fired or not statement.lstrip().upper().startswith("UPDATE INGESTION_TASKS"):
            return
        fired.append(statement)
        # ``with connection`` only commits; ``closing`` also releases the handle.
        with closing(sqlite3.connect(database)) as other, other:
            other.execute(
                "UPDATE ingestion_tasks SET summary = ? WHERE task_id = ?",
                (json.dumps(summary), task_id),
            )

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    return fired


@pytest.mark.asyncio
async def test_fail_if_unsubmitted_does_not_fail_a_row_claimed_after_its_read(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    failure = {"failed_stage": "submission", "error_code": "submission_failed"}
    async with control_plane:
        await _pending(control_plane, "task-a", {})
        claim = {
            "submission_state": "submitting",
            "submission_claimed_at": _claimed_at(timedelta(seconds=0)),
        }
        fired = _commit_before_the_update(
            control_plane, tmp_path / "control-plane.db", "task-a", claim
        )

        outcome = await control_plane.tasks.fail_if_unsubmitted("task-a", summary=failure)

        assert fired
        assert outcome is IngestionTaskState.PENDING
        task = await control_plane.tasks.get("task-a")
        assert task is not None
        assert task.status is IngestionTaskState.PENDING
        assert task.summary == claim


@pytest.mark.asyncio
async def test_claim_submission_loses_to_a_claim_committed_after_its_read(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        await _pending(control_plane, "task-a", {})
        rival = {
            "submission_state": "submitting",
            "submission_claimed_at": _claimed_at(timedelta(seconds=0)),
        }
        fired = _commit_before_the_update(
            control_plane, tmp_path / "control-plane.db", "task-a", rival
        )

        token = await control_plane.tasks.claim_submission("task-a")

        assert fired
        assert token is None
        task = await control_plane.tasks.get("task-a")
        assert task is not None
        assert task.summary == rival
