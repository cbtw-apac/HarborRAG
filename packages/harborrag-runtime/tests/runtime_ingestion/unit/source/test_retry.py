"""Artifact-first source retry behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
    SourceAdmissionDecision,
    TaskDocumentResult,
)
from harborrag_runtime.ingestion.document.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.source.models import PlannedDocumentRelease
from harborrag_runtime.ingestion.source.service import SourceIngestionService

from ...fixtures.release import build_control_plane, release_request


def _task(task_id: str) -> IngestionTask:
    return IngestionTask(
        task_id=task_id,
        source_scope_id="docs",
        status=IngestionTaskState.PENDING,
        request={"connector_type": "local"},
    )


@pytest.mark.asyncio
async def test_retry_reuses_document_version_without_constructing_connector(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    documents = AsyncMock()
    planned = PlannedDocumentRelease(
        request=release_request(source_version="v1"),
        document_id="document:retry",
    )
    documents.replay.return_value = DocumentReleaseOutcome(
        document_id=planned.document_id,
        document_version_id="document-version:retry",
        decision=SourceAdmissionDecision.UNCHANGED,
    )
    async with control:
        await control.tasks.create(_task("original"))
        await control.tasks.create(_task("retry"))
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id="original",
                document_id=planned.document_id,
                document_version_id="document-version:retry",
                status="failed",
                result={"retryable": True},
            )
        )
        service = SourceIngestionService(control=control, documents=documents)

        result = await service.retry_one(
            retry_task_id="retry",
            original_task_id="original",
            planned=planned,
            connector_factory=lambda: pytest.fail("connector must not be constructed"),
        )

        assert result == "unchanged"
        documents.replay.assert_awaited_once_with(
            planned.request,
            "document-version:retry",
        )
        documents.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_failure_retries_from_the_configured_connector(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    documents = AsyncMock()
    connector = object()
    planned = PlannedDocumentRelease(
        request=release_request(source_version="v1"),
        document_id="document:fetch",
    )
    documents.release.return_value = DocumentReleaseOutcome(
        document_id=planned.document_id,
        document_version_id="document-version:fetched",
        decision=SourceAdmissionDecision.UNCHANGED,
    )
    async with control:
        await control.tasks.create(_task("original"))
        await control.tasks.create(_task("retry"))
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id="original",
                document_id=planned.document_id,
                status="failed",
                result={"retryable": True},
            )
        )
        service = SourceIngestionService(control=control, documents=documents)

        await service.retry_one(
            retry_task_id="retry",
            original_task_id="original",
            planned=planned,
            connector_factory=lambda: connector,
        )

        documents.release.assert_awaited_once_with(planned.request, connector)
        documents.replay.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_retry_marks_task_failed_and_stays_idempotent(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    documents = AsyncMock()
    async with control:
        await control.tasks.create(_task("retry"))
        service = SourceIngestionService(control=control, documents=documents)

        await service.fail_retry("retry", error_code="ChildWorkflowError")

        task = await control.tasks.get("retry")
        assert task.status == IngestionTaskState.FAILED

        # A retried recording activity must not raise on a second delivery.
        await service.fail_retry("retry", error_code="ChildWorkflowError")


@pytest.mark.asyncio
async def test_fail_retry_is_a_no_op_for_an_unknown_task(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    documents = AsyncMock()
    async with control:
        service = SourceIngestionService(control=control, documents=documents)

        await service.fail_retry("does-not-exist", error_code="ChildWorkflowError")
