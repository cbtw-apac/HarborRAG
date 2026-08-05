"""Source-level document release and result recording."""

from __future__ import annotations

import logging

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.registry import connector_registry
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import BindingKind, TaskDocumentResult
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from ..document.models import DocumentReleaseOutcome
from ..document.service import DocumentReleaseService
from .models import PlannedDocumentRelease

logger = logging.getLogger("harborrag.runtime.ingestion.source_documents")


class SourceDocumentService:
    """Release one document and persist its bounded public task outcome."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        documents: DocumentReleaseService,
    ) -> None:
        self._control = control
        self._documents = documents

    async def release_one(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        connector: BaseConnector | HarborConnector,
    ) -> str:
        try:
            return await self.publish_one(task_id, planned, connector)
        except Exception as error:
            await self.record_failed_document(
                task_id,
                planned,
                error_type=type(error).__name__,
            )
            return "failed"

    async def publish_one(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        connector: BaseConnector | HarborConnector,
    ) -> str:
        outcome = await self._documents.release(planned.request, connector)
        return await self.record_published_document(task_id, planned, outcome)

    async def record_published_document(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        outcome: DocumentReleaseOutcome,
    ) -> str:
        status = "published" if outcome.published else "unchanged"
        await self._control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=DocumentId(planned.document_id),
                document_version_id=(
                    DocumentVersionId(outcome.document_version_id)
                    if outcome.document_version_id is not None
                    else None
                ),
                status=status,
                result={
                    **self._document_summary(planned),
                    "decision": outcome.decision.value,
                    "evidence_chunks": outcome.evidence_chunks,
                    "graph_nodes": outcome.graph_nodes,
                    "graph_relations": outcome.graph_relations,
                },
            )
        )
        logger.info(
            "Document ingestion completed task_id=%s document_id=%s status=%s decision=%s",
            task_id,
            planned.document_id,
            status,
            outcome.decision.value,
        )
        return status

    async def record_failed_document(
        self,
        task_id: str,
        planned: PlannedDocumentRelease,
        *,
        error_type: str,
        failed_stage: str = "FetchAndCaptureRaw",
    ) -> None:
        normalized = error_type.strip().lower()
        if not normalized:
            raise ValueError("document failure error type must be non-empty")
        safe_error_code = f"document_release_{normalized}"
        await self._control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=DocumentId(planned.document_id),
                status="failed",
                result={
                    **self._document_summary(planned),
                    "safe_error_code": safe_error_code,
                    "failure_stage": failed_stage,
                    "retryable": self._retryable_failure_stage(failed_stage),
                },
            )
        )
        logger.warning(
            "Document ingestion failed task_id=%s document_id=%s stage=%s error_code=%s",
            task_id,
            planned.document_id,
            failed_stage,
            safe_error_code,
        )

    @staticmethod
    def _document_summary(planned: PlannedDocumentRelease) -> dict[str, object]:
        identity = planned.request.source_identity
        source = planned.request.source
        if identity.binding.kind == BindingKind.ATTACHMENT:
            document_kind = "attachment"
        else:
            document_kind = connector_registry.get_definition(
                identity.connector_type.value
            ).document_kind
        title_value = source.metadata.get("title") or source.metadata.get("filename")
        title = str(title_value).strip() if title_value is not None else None
        return {
            "source_item_id": identity.source_item_id,
            "document_kind": document_kind,
            "title": title,
        }

    @staticmethod
    def _retryable_failure_stage(stage: str) -> bool:
        return stage not in {"PersistCanonical", "ChunkAndValidate"}
