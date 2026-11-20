"""Facade for source discovery, dispatch, retry, and finalization.

Every terminal summary stored here replaces the task's summary, so each one
keeps the discovery skip counts that status views read after the run ends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.errors import HarborStorageValidationError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_engine.ingestion.release.service import DocumentReleaseService
from harborrag_engine.ingestion.source.models import (
    DISCOVERY_SKIPS_KEY,
    PlannedDocuments,
    RelationRepairProgress,
    SourceDiscoveryPage,
    SourceDiscoveryRun,
    SourceDispatchSummary,
    SourceIngestionOutcome,
    SourceIngestionRequest,
)
from harborrag_engine.ingestion.source.tasks import pending_source_task, source_scan_id

from ...errors import VectorIndexSchemaError
from ..maintenance.relation_repair import GraphRelationRepairService
from .discovery import SourceDiscoveryPlanner
from .documents import SourceDocumentService
from .finalization import SourceFinalizationService
from .operations import SourceDocumentOperations
from .retry import SourceRetryService
from .skips import carried_discovery_skips

logger = logging.getLogger("harborrag.runtime.ingestion.source")


class SourceIngestionService(SourceDocumentOperations):
    """Coordinate authoritative discovery and bounded document dispatch.

    Args:
        control: Control-plane database that holds tasks, scans and results.
        documents: Release service that ingests one document.
        relations: Graph relation repair. Without it, finalization skips repair.
    """

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
        """Discover, dispatch and finalize one source run in this process.

        A cancellation, such as Ctrl-C on a direct run, cancels the task before it
        propagates. Left RUNNING, the run's scan would block every later scan of
        the scope, and a PENDING task would look like an unsubmitted durable run.
        Any other error marks the task failed. A failure to record either outcome
        is logged and never hides the run's own error.
        """
        logger.info(
            "Source ingestion started task_id=%s connector_type=%s concurrency=%d",
            request.task_id,
            request.connector_type.value,
            request.document_concurrency,
        )
        stage = "discovery"
        try:
            discovery = await self.discover(request, connector)
            logger.info(
                "Source discovery completed task_id=%s scan_id=%s documents=%d",
                request.task_id,
                discovery.scan_id,
                len(discovery.planned),
            )
            stage = "dispatch"
            results = await self._dispatch(
                request,
                connector,
                discovery.planned,
            )
            stage = "finalization"
            outcome = await self.finish(
                request,
                scan_id=discovery.scan_id,
                planned=PlannedDocuments.of(discovery.planned),
                summary=SourceDispatchSummary.from_results(results),
            )
        except asyncio.CancelledError:
            logger.warning(
                "Source ingestion interrupted task_id=%s stage=%s", request.task_id, stage
            )
            try:
                await self.cancel(request.task_id)
            except Exception:  # noqa: BLE001 - never mask the cancellation itself
                logger.exception(
                    "Source cancellation could not be persisted task_id=%s", request.task_id
                )
            raise
        except Exception as error:
            stage = _failure_stage(error, stage)
            logger.error(
                "Source %s failed task_id=%s connector_type=%s error_type=%s",
                stage,
                request.task_id,
                request.connector_type.value,
                type(error).__name__,
            )
            try:
                await self.fail(
                    request.task_id,
                    error_code=f"{stage}_{type(error).__name__.lower()}",
                    stage=stage,
                )
            except Exception:  # noqa: BLE001 - never mask the run's own error
                logger.exception(
                    "Source failure could not be persisted task_id=%s", request.task_id
                )
            raise error
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
        """Converge a graceful workflow cancellation on durable task state.

        The open scan is cancelled first, so a cancel during discovery cannot
        block later scans of the scope. The task keeps its summary.
        """
        if await self._control.source_scans.cancel_if_started(source_scan_id(task_id)):
            logger.info("Open source scan cancelled task_id=%s", task_id)
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
            summary={**task.summary, "cancelled_at_safe_boundary": True},
        )
        logger.info("Source ingestion cancelled task_id=%s", task_id)

    async def pause(self, task_id: str) -> None:
        """Pause a running ingestion task; a missing or paused task is left as is."""
        task = await self._control.tasks.get(task_id)
        if task is None or task.status == IngestionTaskState.PAUSED:
            return
        if task.status == IngestionTaskState.RUNNING:
            await self._control.tasks.transition(task_id, IngestionTaskState.PAUSED)

    async def resume(self, task_id: str) -> None:
        """Resume a paused ingestion task; a missing or running task is left as is."""
        task = await self._control.tasks.get(task_id)
        if task is None or task.status == IngestionTaskState.RUNNING:
            return
        if task.status == IngestionTaskState.PAUSED:
            await self._control.tasks.transition(task_id, IngestionTaskState.RUNNING)

    async def fail(self, task_id: str, *, error_code: str, stage: str = "discovery") -> None:
        """Persist one terminal source-workflow failure without runtime details.

        The failed task keeps the counts of what discovery skipped, so status
        views still show them. A task that already ended is left as it is.
        """
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
                "failed_stage": stage,
                "error_code": error_code,
                **carried_discovery_skips(task.summary),
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
        """Record discovery counts on the task summary."""
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

    async def discovery_skip_counts(self, task_id: str) -> dict[str, int]:
        """Return the discovery-time skip counts recorded on the task so far."""
        task = await self._control.tasks.get(task_id)
        if task is None:
            return {}
        reasons = task.summary.get(DISCOVERY_SKIPS_KEY)
        if not isinstance(reasons, Mapping):
            return {}
        return {
            str(reason): int(count)
            for reason, count in reasons.items()
            if isinstance(count, int) and not isinstance(count, bool)
        }

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
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
        repair_progress: RelationRepairProgress | None = None,
    ) -> SourceIngestionOutcome:
        """Close the source ingestion once every planned document has an outcome."""
        return await self._finalization.finish(
            request,
            scan_id=scan_id,
            planned=planned,
            summary=summary,
            repair_progress=repair_progress,
        )

    async def _initialize(self, request: SourceIngestionRequest) -> None:
        """Register the task and scan scope before discovery starts.

        The task executes in this process, so it is never awaiting submission: the marker keeps the
        API outbox from re-submitting it if it stops while PENDING. A row a durable start registered
        already exists and keeps its summary. The marker is set after the task exists and runs, so a
        provisioning failure can be recorded.
        """
        await self._control.source_scans.register_scope(
            tenant_id=request.tenant_id,
            source_scope_id=request.source_scope_id,
            connector_type=request.connector_type.value,
            connection_id=request.connection_id,
            configuration_fingerprint=request.configuration_fingerprint,
        )
        await self._control.tasks.create(
            pending_source_task(request).model_copy(
                update={"summary": {"submission_state": "executor"}}
            )
        )
        await self._control.tasks.transition(
            request.task_id,
            IngestionTaskState.RUNNING,
        )
        try:
            await self._documents.provision(tenant_id=request.tenant_id)
        except HarborStorageValidationError as error:
            dimensions = _schema_mismatch_dimensions(error)
            if dimensions is None:
                raise
            raise VectorIndexSchemaError(_schema_mismatch_message(error, *dimensions)) from error


def _failure_stage(error: Exception, stage: str) -> str:
    """Return the stage to record for a run that failed while in ``stage``.

    Provisioning runs inside discovery's first step, but a vector index schema
    mismatch is a configuration fault, not a discovery one.
    """
    return "provision" if isinstance(error, VectorIndexSchemaError) else stage


def _schema_mismatch_dimensions(error: HarborStorageValidationError) -> tuple[int, int] | None:
    """Both dimensions when ``error`` is the existing-index schema mismatch, else None.

    Only that mismatch is a configuration verdict the operator can act on; other
    validation failures (an invalid tenant or index name, say) keep their type.
    """
    if error.context.operation != "ensure_index":
        return None
    existing = error.context.metadata.get("existing_dimension")
    configured = error.context.metadata.get("configured_dimension")
    if isinstance(existing, int) and isinstance(configured, int):
        return existing, configured
    return None


def _schema_mismatch_message(
    error: HarborStorageValidationError, existing: int, configured: int
) -> str:
    """Authored text plus the safe values the adapter attached, never its message."""
    index = error.context.resource_name
    subject = f"Vector index {index!r}" if isinstance(index, str) and index else "The vector index"
    detail = (
        f"existing dimension {existing}, configured dimension {configured}"
        if existing != configured
        else f"dimension {existing}; the distance or vector names differ"
    )
    return (
        f"{subject} already exists with a different vector schema ({detail}). "
        "Configure the embedding model and dimension to match it, or use a new "
        "collection and re-ingest."
    )
