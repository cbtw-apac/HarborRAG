"""Document dispatch and retry operations exposed by source ingestion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from itertools import islice

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.ingestion import BindingKind, DocumentIngestionOutcome
from harborrag_engine.ingestion.release.models import DocumentReleaseOutcome
from harborrag_engine.ingestion.source.models import (
    PlannedDocumentRelease,
    SourceDispatchSummary,
    SourceIngestionRequest,
)

from .documents import SourceDocumentService
from .retry import SourceRetryService

logger = logging.getLogger("harborrag.runtime.ingestion.source")


class SourceDocumentOperations:
    """Delegate document results and retries, and bound direct dispatch."""

    _document_results: SourceDocumentService
    _retries: SourceRetryService

    async def release_one(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        connector: BaseConnector | HarborConnector,
    ) -> DocumentIngestionOutcome:
        """Release one planned document and record its outcome."""
        return await self._document_results.release_one(task_id, planned, connector)

    async def record_published_document(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        outcome: DocumentReleaseOutcome,
    ) -> DocumentIngestionOutcome:
        """Record the outcome of a document that was released."""
        return await self._document_results.record_published_document(
            task_id,
            planned,
            outcome,
        )

    async def record_failed_document(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        *,
        error_type: str,
        failed_stage: str = "FetchAndCaptureRaw",
        skip_reason: str | None = None,
    ) -> None:
        """Record a document whose release failed."""
        await self._document_results.record_failed_document(
            task_id,
            planned,
            error_type=error_type,
            failed_stage=failed_stage,
            skip_reason=skip_reason,
        )

    async def begin_retry(self, task_id: str, *, selected: int) -> None:
        """Mark a retry task running for ``selected`` documents."""
        await self._retries.begin_retry(task_id, selected=selected)

    async def fail_retry(self, task_id: str, *, error_code: str) -> None:
        """Mark a retry task failed with ``error_code``."""
        await self._retries.fail_retry(task_id, error_code=error_code)

    async def retry_one(
        self,
        *,
        retry_task_id: str,
        original_task_id: str,
        planned: PlannedDocumentRelease,
        connector_factory: Callable[[], Awaitable[BaseConnector | HarborConnector]],
    ) -> DocumentIngestionOutcome:
        """Retry one failed document under a retry task."""
        return await self._retries.retry_one(
            retry_task_id=retry_task_id,
            original_task_id=original_task_id,
            planned=planned,
            connector_factory=connector_factory,
        )

    async def record_retry_failure(
        self,
        *,
        retry_task_id: str,
        original_task_id: str,
        planned: PlannedDocumentRelease,
        error_type: str,
        skip_reason: str | None = None,
    ) -> None:
        """Record a document that failed again during a retry."""
        await self._retries.record_retry_failure(
            retry_task_id=retry_task_id,
            original_task_id=original_task_id,
            planned=planned,
            error_type=error_type,
            skip_reason=skip_reason,
        )

    async def finish_retry(
        self,
        task_id: str,
        *,
        selected: int,
        summary: SourceDispatchSummary,
    ) -> None:
        """Close a retry task with the summary of its documents."""
        await self._retries.finish_retry(
            task_id,
            selected=selected,
            summary=summary,
        )

    async def _dispatch(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        planned: tuple[PlannedDocumentRelease, ...],
    ) -> tuple[DocumentIngestionOutcome, ...]:
        async def release(item: PlannedDocumentRelease) -> DocumentIngestionOutcome:
            return await self.release_one(request.task_id, item, connector)

        async def release_bounded(
            items: Iterable[PlannedDocumentRelease],
        ) -> tuple[DocumentIngestionOutcome, ...]:
            results: list[DocumentIngestionOutcome] = []
            iterator = iter(items)
            while batch := tuple(islice(iterator, request.document_concurrency)):
                results.extend(await asyncio.gather(*(release(item) for item in batch)))
            return tuple(results)

        roots = (
            item
            for item in planned
            if item.request.source_identity.binding.kind == BindingKind.ROOT
        )
        bound = (
            item
            for item in planned
            if item.request.source_identity.binding.kind != BindingKind.ROOT
        )
        root_count = sum(
            item.request.source_identity.binding.kind == BindingKind.ROOT for item in planned
        )
        logger.info(
            "Source dispatch started task_id=%s roots=%d bound=%d concurrency=%d",
            request.task_id,
            root_count,
            len(planned) - root_count,
            request.document_concurrency,
        )
        results = (*await release_bounded(roots), *await release_bounded(bound))
        summary = SourceDispatchSummary.from_results(results)
        logger.info(
            "Source dispatch completed task_id=%s published=%d unchanged=%d failed=%d",
            request.task_id,
            summary.published,
            summary.unchanged,
            summary.failed,
        )
        return results
