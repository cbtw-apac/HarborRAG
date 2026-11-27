"""Temporal activities for source-level ingestion steps."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from time import perf_counter

import temporalio
from temporalio import activity
from temporalio.api.workflowservice.v1 import (
    GetSystemInfoRequest,
    PauseWorkflowExecutionRequest,
    UnpauseWorkflowExecutionRequest,
)
from temporalio.client import Client
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory
from temporalio.service import RPCError, RPCStatusCode

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion.run_contracts import RunKind
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion.source.models import (
    RelationRepairProgress,
    SourceDiscoveryRun,
    SourceDispatchSummary,
    SourceIngestionRequest,
    SourcePlanCheckpoint,
    SourcePlanIndex,
)
from harborrag_engine.ingestion.source.tasks import source_scan_id
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.source.skips import run_skip_summary

from .activity_observability import ActivityObservability
from .conversion import to_artifact_reference, to_source_request, to_workflow_artifact
from .heartbeats import heartbeat_while, last_heartbeat_detail
from .schemas import (
    SourceCancellationInput,
    SourceCancellationResult,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourcePauseInput,
    SourceResumeInput,
    WorkflowExecutionControlInput,
    reported_removal_candidates,
)

logger = logging.getLogger("harborrag.runtime.temporal.source_activities")

# Finish RPC work before the workflow's five-second native-pause activity can time out.
_NATIVE_PAUSE_RPC_BUDGET_SECONDS = 4
_NATIVE_PAUSE_RPC_TIMEOUT = timedelta(seconds=2)


def _version_at_least(version: str, minimum: tuple[int, int, int]) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    return match is not None and tuple(map(int, match.groups())) >= minimum


class SourceActivitiesMixin:
    """Source discovery, cancellation, failure, and finalization boundaries."""

    _runtime: IngestionRuntime
    _observability: ActivityObservability
    _temporal_client: Client | None

    @activity.defn(name="harborrag.close_cancelled_source_execution")
    async def close_cancelled_source_execution(self) -> None:
        """Deliver native cancellation after the source workflow has drained and persisted."""
        if self._temporal_client is None:
            raise RuntimeError("Temporal client is required for workflow cancellation")
        info = activity.info()
        await self._temporal_client.get_workflow_handle(
            info.workflow_id, run_id=info.workflow_run_id
        ).cancel()

    @activity.defn(name="harborrag.read_cancelled_source_result")
    async def read_cancelled_source_result(
        self, request: SourceCancellationInput
    ) -> SourceIngestionResult | None:
        """Read a scheduled source's graceful cancellation summary from its closed execution."""
        if self._temporal_client is None:
            raise RuntimeError("Temporal client is required to read a cancellation result")
        return await self._temporal_client.get_workflow_handle(
            RunKind.SOURCE.workflow_id(request.task_id)
        ).query("get_graceful_cancel_result", result_type=SourceIngestionResult | None)

    @activity.defn(name="harborrag.pause_workflow_execution")
    async def pause_workflow_execution(self, request: WorkflowExecutionControlInput) -> None:
        """Pause the workflow execution.

        A batch that already closed (or is already paused) has nothing to pause; retrying would only
        hold up the source workflow's relay.
        """
        if self._temporal_client is None:
            raise RuntimeError("Temporal client is required for workflow pause control")
        info = activity.info()
        try:
            async with asyncio.timeout(_NATIVE_PAUSE_RPC_BUDGET_SECONDS):
                if request.run_id and not await self._supports_native_pause(self._temporal_client):
                    return
                await self._temporal_client.workflow_service.pause_workflow_execution(
                    PauseWorkflowExecutionRequest(
                        namespace=self._temporal_client.namespace,
                        workflow_id=request.workflow_id,
                        run_id=request.run_id or "",
                        identity="harborrag-runtime",
                        reason="Source ingestion pause requested",
                        request_id=f"{info.workflow_id}:{info.activity_id}",
                    ),
                    timeout=_NATIVE_PAUSE_RPC_TIMEOUT,
                )
        except RPCError as error:
            if error.status in {
                RPCStatusCode.FAILED_PRECONDITION,
                RPCStatusCode.NOT_FOUND,
                RPCStatusCode.UNIMPLEMENTED,
            }:
                return
            raise

    @staticmethod
    async def _supports_native_pause(client: Client) -> bool:
        if not _version_at_least(temporalio.__version__, (1, 37, 0)):
            return False
        system = await client.workflow_service.get_system_info(
            GetSystemInfoRequest(), timeout=_NATIVE_PAUSE_RPC_TIMEOUT
        )
        return _version_at_least(system.server_version, (1, 32, 2))

    @activity.defn(name="harborrag.unpause_workflow_execution")
    async def unpause_workflow_execution(self, request: WorkflowExecutionControlInput) -> None:
        """Unpause the workflow execution."""
        if self._temporal_client is None:
            raise RuntimeError("Temporal client is required for workflow pause control")
        info = activity.info()
        try:
            await self._temporal_client.workflow_service.unpause_workflow_execution(
                UnpauseWorkflowExecutionRequest(
                    namespace=self._temporal_client.namespace,
                    workflow_id=request.workflow_id,
                    identity="harborrag-runtime",
                    reason="Source ingestion resume requested",
                    request_id=f"{info.workflow_id}:{info.activity_id}",
                )
            )
        except RPCError as error:
            if error.status in {
                RPCStatusCode.FAILED_PRECONDITION,
                RPCStatusCode.NOT_FOUND,
                RPCStatusCode.UNIMPLEMENTED,
            }:
                return
            raise

    @activity.defn(name="harborrag.discover_source_items")
    async def discover_source_items(
        self,
        source: SourceIngestionInput,
    ) -> SourceDiscoveryResult:
        """Discover the source's items and store the plan.

        The plan is its page index, written last: paged discovery already persisted every page, and
        an unpaged one is paged here. Nothing writes, or holds, the whole plan at once.

        A Temporal cancel closes the run's scan and cancels the run within a second, but this
        attempt hears of it only at its next heartbeat. A walk of a cancelled run that meets its
        closed scan first stops without logging a failure, because nothing can use its result.

        Raises:
            ApplicationError: Benign and non-retryable when the run was cancelled under the walk.
        """
        with self._observability.boundary("DiscoverSourceItems"):
            try:
                return await self._discover_source_items(source)
            except HarborConflictError:
                if not await self._runtime.sources.is_cancelled(source.task_id):
                    raise
                logger.info(
                    "Source discovery stopped; the run was cancelled task_id=%s", source.task_id
                )
        raise ApplicationError(
            "source discovery stopped because the run was cancelled",
            type="source_discovery_cancelled",
            non_retryable=True,
            category=ApplicationErrorCategory.BENIGN,
        )

    async def _discover_source_items(self, source: SourceIngestionInput) -> SourceDiscoveryResult:
        """Replay the stored plan, or walk the source and store a new one."""
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
            replayed = await self._runtime.source_plans.documents(replay, context=context)
            await self._runtime.sources.complete_discovery(scan_id)
            await self._runtime.sources.record_discovery_planned(
                source.task_id,
                replayed.document_count,
            )
            self._observability.record_discovery(
                source.connector_type,
                replayed.document_count,
                replayed=True,
            )
            logger.info(
                "Source discovery plan replayed task_id=%s scan_id=%s documents=%d",
                source.task_id,
                scan_id,
                replayed.document_count,
            )
            return SourceDiscoveryResult(
                scan_id=scan_id,
                plan_reference=to_workflow_artifact(replay),
                document_count=replayed.document_count,
                skipped_by_reason=await self._runtime.sources.discovery_skip_counts(source.task_id),
            )
        connector = await self._runtime.connector(
            tenant_id=source.tenant_id,
            task_id=source.task_id,
            configuration_fingerprint=source.configuration_fingerprint,
            run_settings=source.run_settings,
        )
        health = self._runtime.connection_health
        try:
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
        except Exception as error:
            await health.record_failure(
                tenant_id=source.tenant_id, source_key=source.connection_id, error=error
            )
            raise
        await health.record_success(tenant_id=source.tenant_id, source_key=source.connection_id)
        if discovery.page_counts:
            index = SourcePlanIndex.from_page_counts(discovery.page_counts)
            reference = await self._runtime.source_plans.put_index(
                task_id=source.task_id,
                scan_id=discovery.scan_id,
                index=index,
                context=context,
            )
            document_count = index.document_count
        else:
            reference = await self._runtime.source_plans.put_pages_and_index(
                task_id=source.task_id,
                scan_id=discovery.scan_id,
                planned=discovery.planned,
                context=context,
            )
            document_count = len(discovery.planned)
        await self._runtime.sources.complete_discovery(discovery.scan_id)
        await self._runtime.sources.record_discovery_planned(
            source.task_id,
            document_count,
        )
        self._observability.record_discovery(
            source.connector_type,
            document_count,
            replayed=False,
        )
        logger.info(
            "Source discovery completed task_id=%s scan_id=%s documents=%d",
            source.task_id,
            discovery.scan_id,
            document_count,
        )
        return SourceDiscoveryResult(
            scan_id=discovery.scan_id,
            plan_reference=to_workflow_artifact(reference),
            document_count=document_count,
            skipped_by_reason=await self._runtime.sources.discovery_skip_counts(source.task_id),
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
        """Resume native discovery from immutable per-page cursor checkpoints.

        Only page counts are kept: each page's documents are already in its
        checkpoint, and the plan index written from these counts is how every
        later reader finds them.
        """
        await self._runtime.sources.begin_discovery(request)
        document_count = 0
        page_counts: list[int] = []
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
                    return SourceDiscoveryRun(
                        scan_id=scan_id, planned=(), page_counts=tuple(page_counts)
                    )
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
            document_count += len(page_planned)
            page_counts.append(len(page_planned))
            root_count += page_roots
            page_number += 1
            await self._runtime.sources.record_discovery_progress(
                source.task_id,
                root_count=root_count,
                document_count=document_count,
                page_count=page_number,
            )
            try:
                activity.heartbeat(
                    {
                        "stage": "discovery",
                        "page": page_number,
                        "roots": root_count,
                        "documents": document_count,
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
                document_count,
                replayed,
                duration * 1000,
            )
            if next_cursor is None:
                return SourceDiscoveryRun(
                    scan_id=scan_id, planned=(), page_counts=tuple(page_counts)
                )
            if next_cursor == cursor:
                raise ValueError("connector returned a non-advancing discovery cursor")
            cursor = next_cursor

    @activity.defn(name="harborrag.cancel_source_ingestion")
    async def cancel_source_ingestion(
        self, request: SourceCancellationInput
    ) -> SourceCancellationResult:
        """Cancel the source ingestion and return its skips at discovery and per document."""
        with self._observability.boundary("CancelSourceIngestion"):
            await self._runtime.sources.cancel(request.task_id)
            skips = await run_skip_summary(self._runtime.control, request.task_id)
            return SourceCancellationResult(skipped_by_reason=skips["skipped_by_reason"])

    @activity.defn(name="harborrag.pause_source_ingestion")
    async def pause_source_ingestion(self, request: SourcePauseInput) -> None:
        """Pause the source ingestion."""
        with self._observability.boundary("PauseSourceIngestion"):
            await self._runtime.sources.pause(request.task_id)

    @activity.defn(name="harborrag.resume_source_ingestion")
    async def resume_source_ingestion(self, request: SourceResumeInput) -> None:
        """Resume the source ingestion."""
        with self._observability.boundary("ResumeSourceIngestion"):
            await self._runtime.sources.resume(request.task_id)

    @activity.defn(name="harborrag.record_source_failure")
    async def record_source_failure(self, request: SourceFailureInput) -> None:
        """Record the source ingestion failure."""
        with self._observability.boundary("RecordSourceFailure"):
            await self._runtime.sources.fail(request.task_id, error_code=request.error_code)

    @activity.defn(name="harborrag.finalize_source_ingestion")
    async def finalize_source_ingestion(
        self,
        request: SourceFinalizationInput,
    ) -> SourceIngestionResult:
        """Finalize the source ingestion.

        Relation repair and removal reconciliation scale with the source, so a large one runs well
        past any single fixed budget; the heartbeat is what tells a slow finalization from a dead
        worker. It carries repair progress, so a retry resumes where this one stopped.
        """
        with self._observability.boundary("FinalizeSourceIngestion"):
            source = to_source_request(request.source)
            planned = await self._runtime.source_plans.documents(
                to_artifact_reference(request.plan_reference),
                context=StorageOperationContext.system(request.source.tenant_id),
            )
            progress = RelationRepairProgress.resume(last_heartbeat_detail())
            outcome = await heartbeat_while(
                self._runtime.sources.finish(
                    source,
                    scan_id=request.scan_id,
                    planned=planned,
                    summary=SourceDispatchSummary(
                        published=request.summary.published,
                        unchanged=request.summary.unchanged,
                        failed=request.summary.failed,
                    ),
                    repair_progress=progress,
                ),
                detail=progress,
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
                removal_candidates=reported_removal_candidates(outcome.removal_candidates),
                removal_count=len(outcome.removal_candidates),
                unresolved_relations=outcome.unresolved_relations,
                status=outcome.status.value,
                skipped=outcome.skipped,
                skipped_by_reason=outcome.skipped_by_reason,
            )
