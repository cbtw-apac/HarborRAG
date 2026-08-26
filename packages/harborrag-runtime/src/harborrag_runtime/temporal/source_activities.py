from __future__ import annotations

import logging
from time import perf_counter

from temporalio import activity

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.source.models import (
    PlannedDocumentRelease,
    SourceDiscoveryRun,
    SourceDispatchSummary,
    SourceIngestionRequest,
    SourcePlanCheckpoint,
)
from harborrag_runtime.ingestion.source.tasks import source_scan_id

from .activity_observability import ActivityObservability
from .conversion import to_artifact_reference, to_source_request, to_workflow_artifact
from .heartbeats import heartbeat_while
from .schemas import (
    SourceCancellationInput,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
)

logger = logging.getLogger("harborrag.runtime.temporal.source_activities")


class SourceActivitiesMixin:
    """Source discovery, cancellation, failure, and finalization boundaries."""

    _runtime: IngestionRuntime
    _observability: ActivityObservability

    @activity.defn(name="harborrag.discover_source_items")
    async def discover_source_items(
        self,
        source: SourceIngestionInput,
    ) -> SourceDiscoveryResult:
        with self._observability.boundary("DiscoverSourceItems"):
            logger.info(
                "Source discovery started task_id=%s tenant=%s connector=%s",
                source.task_id,
                source.tenant_id,
                source.connector_name,
            )
            request = to_source_request(source)
            context = StorageOperationContext.system(source.tenant_id)
            scan_id = source_scan_id(source.task_id)
            replay = await self._runtime.source_plans.find(
                task_id=source.task_id,
                scan_id=scan_id,
                context=context,
            )
            if replay is not None:
                planned = await self._runtime.source_plans.get(replay, context=context)
                await self._runtime.sources.complete_discovery(scan_id)
                await self._runtime.sources.record_discovery_planned(
                    source.task_id,
                    len(planned),
                )
                self._observability.record_discovery(
                    source.connector_type,
                    len(planned),
                    replayed=True,
                )
                logger.info(
                    "Source discovery plan replayed task_id=%s scan_id=%s documents=%d",
                    source.task_id,
                    scan_id,
                    len(planned),
                )
                return SourceDiscoveryResult(
                    scan_id=scan_id,
                    plan_reference=to_workflow_artifact(replay),
                    document_count=len(planned),
                )
            connector = self._runtime.connector(
                source.connector_name,
                configuration_fingerprint=source.configuration_fingerprint,
            )
            if bool(getattr(getattr(connector, "capabilities", None), "pagination", False)):
                discovery = await self._discover_checkpointed_pages(
                    source,
                    request=request,
                    connector=connector,
                    context=context,
                    scan_id=scan_id,
                )
            else:
                discovery = await heartbeat_while(
                    self._runtime.sources.prepare_discovery(
                        request,
                        connector,
                    ),
                    detail="discover-legacy-source",
                )
            reference = await self._runtime.source_plans.put(
                task_id=source.task_id,
                scan_id=discovery.scan_id,
                planned=discovery.planned,
                context=context,
            )
            await self._runtime.sources.complete_discovery(discovery.scan_id)
            await self._runtime.sources.record_discovery_planned(
                source.task_id,
                len(discovery.planned),
            )
            self._observability.record_discovery(
                source.connector_type,
                len(discovery.planned),
                replayed=False,
            )
            logger.info(
                "Source discovery completed task_id=%s scan_id=%s documents=%d",
                source.task_id,
                discovery.scan_id,
                len(discovery.planned),
            )
            return SourceDiscoveryResult(
                scan_id=discovery.scan_id,
                plan_reference=to_workflow_artifact(reference),
                document_count=len(discovery.planned),
            )

    async def _discover_checkpointed_pages(
        self,
        source: SourceIngestionInput,
        *,
        request: SourceIngestionRequest,
        connector: BaseConnector | HarborConnector,
        context: StorageOperationContext,
        scan_id: str,
    ) -> SourceDiscoveryRun:
        """Resume native discovery from immutable per-page cursor checkpoints."""

        await self._runtime.sources.begin_discovery(request)
        planned: list[PlannedDocumentRelease] = []
        cursor: str | None = None
        root_count = 0
        page_number = 0
        while True:
            checkpoint_reference = await self._runtime.source_plans.find_page(
                task_id=source.task_id,
                scan_id=scan_id,
                page_number=page_number,
                context=context,
            )
            if checkpoint_reference is not None:
                checkpoint = await self._runtime.source_plans.get_page(
                    checkpoint_reference,
                    context=context,
                )
                page_planned = checkpoint.planned
                next_cursor = checkpoint.next_cursor
                replayed = True
                page_roots = checkpoint.root_count
                duration = 0.0
            else:
                started_at = perf_counter()
                remaining = (
                    request.query.limit - root_count if request.query.limit is not None else None
                )
                if remaining is not None and remaining <= 0:
                    return SourceDiscoveryRun(scan_id=scan_id, planned=tuple(planned))
                page = await heartbeat_while(
                    self._runtime.sources.discover_page(
                        request,
                        connector,
                        scan_id=scan_id,
                        cursor=cursor,
                        page_size=(
                            min(request.discovery_page_size, remaining)
                            if remaining is not None
                            else request.discovery_page_size
                        ),
                    ),
                    detail=f"discover-page:{page_number}",
                )
                duration = perf_counter() - started_at
                page_planned = page.planned
                next_cursor = (
                    None
                    if request.query.limit is not None
                    and root_count + page.root_count >= request.query.limit
                    else page.next_cursor
                )
                page_roots = page.root_count
                replayed = False
                checkpoint = SourcePlanCheckpoint(
                    planned=page_planned,
                    next_cursor=next_cursor,
                    root_count=page_roots,
                )
                await self._runtime.source_plans.put_page(
                    task_id=source.task_id,
                    scan_id=scan_id,
                    page_number=page_number,
                    checkpoint=checkpoint,
                    context=context,
                )
            self._observability.record_discovery_page(
                source.connector_type,
                root_count=page_roots,
                duration_seconds=duration,
                replayed=replayed,
            )
            planned.extend(page_planned)
            root_count += page_roots
            page_number += 1
            await self._runtime.sources.record_discovery_progress(
                source.task_id,
                root_count=root_count,
                document_count=len(planned),
                page_count=page_number,
            )
            try:
                activity.heartbeat(
                    {
                        "stage": "discovery",
                        "page": page_number,
                        "roots": root_count,
                        "documents": len(planned),
                        "replayed": replayed,
                    }
                )
            except RuntimeError:
                pass
            logger.info(
                "Source discovery checkpoint task_id=%s page=%d roots=%d "
                "documents=%d replayed=%s duration_ms=%.1f",
                source.task_id,
                page_number,
                root_count,
                len(planned),
                replayed,
                duration * 1000,
            )
            if next_cursor is None:
                return SourceDiscoveryRun(scan_id=scan_id, planned=tuple(planned))
            if next_cursor == cursor:
                raise ValueError("connector returned a non-advancing discovery cursor")
            cursor = next_cursor

    @activity.defn(name="harborrag.cancel_source_ingestion")
    async def cancel_source_ingestion(self, request: SourceCancellationInput) -> None:
        with self._observability.boundary("CancelSourceIngestion"):
            await self._runtime.sources.cancel(request.task_id)

    @activity.defn(name="harborrag.record_source_failure")
    async def record_source_failure(self, request: SourceFailureInput) -> None:
        with self._observability.boundary("RecordSourceFailure"):
            await self._runtime.sources.fail(request.task_id, error_code=request.error_code)

    @activity.defn(name="harborrag.finalize_source_ingestion")
    async def finalize_source_ingestion(
        self,
        request: SourceFinalizationInput,
    ) -> SourceIngestionResult:
        with self._observability.boundary("FinalizeSourceIngestion"):
            source = to_source_request(request.source)
            planned = await self._runtime.source_plans.get(
                to_artifact_reference(request.plan_reference),
                context=StorageOperationContext.system(request.source.tenant_id),
            )
            outcome = await self._runtime.sources.finish(
                source,
                scan_id=request.scan_id,
                planned=planned,
                summary=SourceDispatchSummary(
                    published=request.summary.published,
                    unchanged=request.summary.unchanged,
                    failed=request.summary.failed,
                ),
            )
            logger.info(
                "Source ingestion finalized task_id=%s status=%s discovered=%d "
                "published=%d unchanged=%d failed=%d removals=%d",
                outcome.task_id,
                outcome.status.value,
                outcome.discovered,
                outcome.published,
                outcome.unchanged,
                outcome.failed,
                len(outcome.removal_candidates),
            )
            return SourceIngestionResult(
                task_id=outcome.task_id,
                scan_id=outcome.scan_id,
                discovered=outcome.discovered,
                published=outcome.published,
                unchanged=outcome.unchanged,
                failed=outcome.failed,
                removal_candidates=outcome.removal_candidates,
                unresolved_relations=outcome.unresolved_relations,
                status=outcome.status.value,
            )
