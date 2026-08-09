"""Facade for source discovery, dispatch, retry, and finalization."""

from __future__ import annotations

import logging

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import IngestionTaskState

from ..document.service import DocumentReleaseService
from ..maintenance.relation_repair import GraphRelationRepairService
from .discovery import SourceDiscoveryPlanner
from .documents import SourceDocumentService
from .finalization import SourceFinalizationService
from .models import (
    PlannedDocumentRelease,
    SourceDiscoveryPage,
    SourceDiscoveryRun,
    SourceDispatchSummary,
    SourceIngestionOutcome,
    SourceIngestionRequest,
)
from .operations import SourceDocumentOperations
from .retry import SourceRetryService
from .tasks import pending_source_task, source_scan_id

logger = logging.getLogger("harborrag.runtime.ingestion.source")


class SourceIngestionService(SourceDocumentOperations):
    """Coordinate authoritative discovery and bounded document dispatch."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        documents: DocumentReleaseService,
        relations: GraphRelationRepairService | None = None,
    ) -> None:
        self._control = control
        self._documents = documents
        self._discovery = SourceDiscoveryPlanner(control)
        self._document_results = SourceDocumentService(
            control=control,
            documents=documents,
        )
        self._retries = SourceRetryService(
            control=control,
            documents=documents,
            document_results=self._document_results,
        )
        self._finalization = SourceFinalizationService(
            control=control,
            relations=relations,
        )

    async def ingest(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
    ) -> SourceIngestionOutcome:
        logger.info(
            "Source ingestion started task_id=%s connector_type=%s concurrency=%d",
            request.task_id,
            request.connector_type.value,
            request.document_concurrency,
        )
        try:
            discovery = await self.discover(request, connector)
        except Exception as error:
            logger.error(
                "Source discovery failed task_id=%s connector_type=%s error_type=%s",
                request.task_id,
                request.connector_type.value,
                type(error).__name__,
            )
            await self.fail(
                request.task_id,
                error_code=f"discovery_{type(error).__name__.lower()}",
            )
            raise
        logger.info(
            "Source discovery completed task_id=%s scan_id=%s documents=%d",
            request.task_id,
            discovery.scan_id,
            len(discovery.planned),
        )
        results = await self._dispatch(
            request,
            connector,
            discovery.planned,
        )
        outcome = await self.finish(
            request,
            scan_id=discovery.scan_id,
            planned=discovery.planned,
            summary=SourceDispatchSummary.from_results(results),
        )
        logger.info(
            "Source ingestion completed task_id=%s scan_id=%s status=%s "
            "discovered=%d published=%d unchanged=%d failed=%d removals=%d",
            outcome.task_id,
            outcome.scan_id,
            outcome.status.value,
            outcome.discovered,
            outcome.published,
            outcome.unchanged,
            outcome.failed,
            len(outcome.removal_candidates),
        )
        return outcome

    async def cancel(self, task_id: str) -> None:
        """Converge a graceful workflow cancellation on durable task state."""

        task = await self._control.tasks.get(task_id)
        if task is None or task.status == IngestionTaskState.CANCELLED:
            logger.debug(
                "Source cancellation skipped task_id=%s reason=missing_or_cancelled", task_id
            )
            return
        if task.status in {
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
        }:
            logger.debug(
                "Source cancellation skipped task_id=%s reason=terminal status=%s",
                task_id,
                task.status.value,
            )
            return
        await self._control.tasks.transition(
            task_id,
            IngestionTaskState.CANCELLED,
            summary={"cancelled_at_safe_boundary": True},
        )
        logger.info("Source ingestion cancelled task_id=%s", task_id)

    async def fail(self, task_id: str, *, error_code: str) -> None:
        """Persist one terminal source-workflow failure without runtime details."""

        await self._control.source_scans.fail_if_started(
            source_scan_id(task_id),
            safe_reason=error_code,
        )
        task = await self._control.tasks.get(task_id)
        if task is None or task.status in {
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }:
            logger.debug(
                "Source failure persistence skipped task_id=%s error_code=%s reason=terminal",
                task_id,
                error_code,
            )
            return
        await self._control.tasks.transition(
            task_id,
            IngestionTaskState.FAILED,
            summary={
                "failed_stage": "discovery",
                "error_code": error_code,
            },
        )
        logger.warning(
            "Source ingestion marked failed task_id=%s error_code=%s",
            task_id,
            error_code,
        )

    async def discover(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
    ) -> SourceDiscoveryRun:
        """Run and persist an authoritative descriptor-only source scan."""

        discovery = await self.prepare_discovery(request, connector)
        await self.complete_discovery(discovery.scan_id)
        return discovery

    async def prepare_discovery(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
    ) -> SourceDiscoveryRun:
        """Discover descriptors while leaving the scan open for plan persistence."""

        scan_id = await self.begin_discovery(request)
        planned = await self._discovery.discover(
            request,
            connector,
            scan_id=scan_id,
        )
        logger.debug(
            "Source discovery plan persisted task_id=%s scan_id=%s documents=%d",
            request.task_id,
            scan_id,
            len(planned),
        )
        return SourceDiscoveryRun(scan_id=scan_id, planned=planned)

    async def begin_discovery(self, request: SourceIngestionRequest) -> str:
        """Create or resume the deterministic authoritative scan."""

        await self._initialize(request)
        return await self._control.source_scans.start(
            request.source_scope_id,
            scan_id=source_scan_id(request.task_id),
        )

    async def discover_page(
        self,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        *,
        scan_id: str,
        cursor: str | None,
        page_size: int | None = None,
    ) -> SourceDiscoveryPage:
        """Persist one native provider page without completing the scan."""

        return await self._discovery.discover_page(
            request,
            connector,
            scan_id=scan_id,
            cursor=cursor,
            page_size=page_size,
        )

    async def record_discovery_progress(
        self,
        task_id: str,
        *,
        root_count: int,
        document_count: int,
        page_count: int,
    ) -> None:
        await self._control.tasks.update_summary(
            task_id,
            {
                "stage": "DISCOVERING",
                "discovery_pages": page_count,
                "discovery_roots": root_count,
                "discovered": document_count,
                "admitted": document_count,
            },
        )

    async def complete_discovery(self, scan_id: str) -> None:
        """Make a scan authoritative after its dispatch plan is durable."""

        await self._control.source_scans.complete(scan_id)
        logger.debug("Source discovery scan completed scan_id=%s", scan_id)

    async def record_discovery_planned(self, task_id: str, document_count: int) -> None:
        """Expose durable coarse progress without leaking workflow activities."""

        await self._control.tasks.update_summary(
            task_id,
            {
                "stage": "PROCESSING_DOCUMENTS",
                "discovered": document_count,
                "admitted": document_count,
            },
        )

    async def finish(
        self,
        request: SourceIngestionRequest,
        *,
        scan_id: str,
        planned: tuple[PlannedDocumentRelease, ...],
        summary: SourceDispatchSummary,
    ) -> SourceIngestionOutcome:
        return await self._finalization.finish(
            request,
            scan_id=scan_id,
            planned=planned,
            summary=summary,
        )

    async def _initialize(self, request: SourceIngestionRequest) -> None:
        await self._control.source_scans.register_scope(
            tenant_id=request.tenant_id,
            source_scope_id=request.source_scope_id,
            connector_type=request.connector_type.value,
            connection_id=request.connection_id,
            configuration_fingerprint=request.configuration_fingerprint,
        )
        await self._control.tasks.create(pending_source_task(request))
        await self._control.tasks.transition(
            request.task_id,
            IngestionTaskState.RUNNING,
        )
        await self._documents.provision(tenant_id=request.tenant_id)
