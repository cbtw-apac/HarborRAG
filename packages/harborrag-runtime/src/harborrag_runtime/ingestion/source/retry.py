"""Artifact-first source retry service."""

from __future__ import annotations

from collections.abc import Callable

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import IngestionTaskState

from ..document.service import DocumentReleaseService
from .documents import SourceDocumentService
from .models import PlannedDocumentRelease, SourceDispatchSummary


class SourceRetryService:
    """Resume failed document releases from durable artifacts."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        documents: DocumentReleaseService,
        document_results: SourceDocumentService,
    ) -> None:
        self._control = control
        self._documents = documents
        self._document_results = document_results

    async def finish_retry(
        self,
        task_id: str,
        *,
        selected: int,
        summary: SourceDispatchSummary,
    ) -> None:
        await self._control.tasks.finalize(
            task_id,
            summary.task_state(),
            summary={
                "stage": "COMPLETED",
                "discovered": selected,
                "admitted": selected,
                "published": summary.published,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
                "removal_candidates": 0,
            },
        )

    async def begin_retry(self, task_id: str, *, selected: int) -> None:
        await self._control.tasks.transition(
            task_id,
            IngestionTaskState.RUNNING,
            summary={
                "stage": "PROCESSING_DOCUMENTS",
                "discovered": selected,
                "admitted": selected,
            },
        )

    async def retry_one(
        self,
        *,
        retry_task_id: str,
        original_task_id: str,
        planned: PlannedDocumentRelease,
        connector_factory: Callable[[], BaseConnector | HarborConnector],
    ) -> str:
        previous = await self._control.tasks.document_result(
            original_task_id,
            planned.document_id,
        )
        if previous is None or previous.status.lower() != "failed":
            raise ValueError("retry document is not a failed result of the original task")
        if previous.document_version_id is None:
            outcome = await self._documents.release(planned.request, connector_factory())
        else:
            outcome = await self._documents.replay(
                planned.request,
                str(previous.document_version_id),
            )
        return await self._document_results.record_published_document(
            retry_task_id,
            planned,
            outcome,
        )

    async def record_retry_failure(
        self,
        *,
        retry_task_id: str,
        original_task_id: str,
        planned: PlannedDocumentRelease,
        error_type: str,
    ) -> None:
        previous = await self._control.tasks.document_result(
            original_task_id,
            planned.document_id,
        )
        failed_stage = (
            str(previous.result.get("failure_stage"))
            if previous is not None
            else "FetchAndCaptureRaw"
        )
        await self._document_results.record_failed_document(
            retry_task_id,
            planned,
            error_type=error_type,
            failed_stage=failed_stage,
        )
