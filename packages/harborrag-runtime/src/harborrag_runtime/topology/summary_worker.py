"""Separate task queue and durable source-coalesced dispatch for summary generation."""

import asyncio
import logging
from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from .summary_factory import SummaryRuntimeFactory
from .summary_service import SummaryProjectionService
from .summary_workflow import SummaryProjectionWorkflow, SummaryWorkflowInput

logger = logging.getLogger("harborrag.runtime.topology.summary_worker")
# A failing pass (a database failover, a reset connection) backs the poll off up
# to this ceiling instead of hammering the database every few seconds.
_MAX_BACKOFF_SECONDS = 300.0


class SummaryActivities:
    def __init__(self, service: SummaryProjectionService) -> None:
        self.service = service

    @activity.defn(name="harborrag.project_summaries")
    async def project(self, request: SummaryWorkflowInput) -> str:
        return await self.service.run_once(
            request.tenant_id,
            source_scope_id=request.source_scope_id,
            heartbeat=lambda: activity.heartbeat(request.source_scope_id),
        )


async def serve_summaries(
    client: Client,
    factory: SummaryRuntimeFactory,
    tenant_ids: tuple[str, ...],
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Serve one queue and reconcile every explicitly managed tenant."""
    tenant_ids = tuple(dict.fromkeys(tenant_ids))
    if not tenant_ids:
        return
    settings = factory.settings
    activities = SummaryActivities(factory.service())
    for tenant_id in tenant_ids:
        await factory.initialize(tenant_id)
        await factory.synchronize(tenant_id)
    async with Worker(
        client,
        task_queue=settings.summary_task_queue,
        workflows=[SummaryProjectionWorkflow],
        activities=[activities.project],
        max_concurrent_activities=settings.temporal_max_concurrent_activities,
        graceful_shutdown_timeout=timedelta(seconds=settings.temporal_graceful_shutdown_seconds),
    ):
        failed_passes = 0
        while stop_event is None or not stop_event.is_set():
            # Newly published sources must acquire policy without a worker restart.
            failed = False
            for tenant_id in tenant_ids:
                if not await _dispatch_guarded(client, factory, tenant_id):
                    failed = True
            failed_passes = failed_passes + 1 if failed else 0
            await _poll_delay(_backoff(settings.topology_poll_seconds, failed_passes), stop_event)


async def watch_summaries(
    client: Client,
    factory: SummaryRuntimeFactory,
    tenant_id: str,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Backward-compatible single-tenant entry point for the topology CLI."""
    await serve_summaries(client, factory, (tenant_id,), stop_event=stop_event)


async def _dispatch_guarded(client: Client, factory: SummaryRuntimeFactory, tenant_id: str) -> bool:
    """Dispatch one tenant; a failure is logged, never raised.

    This loop shares its host's ``asyncio.gather`` with every ingestion worker,
    so one escaped error here would shut all of them down. Summaries are an
    optional projection: the next poll retries, and the other tenants go on.
    """

    try:
        await _dispatch(client, factory, tenant_id)
    except Exception:  # noqa: BLE001 - an optional projection must not stop ingestion
        logger.error("Summary dispatch failed tenant_id=%s", tenant_id, exc_info=True)
        return False
    return True


def _backoff(seconds: float, failed_passes: int) -> float:
    if not failed_passes:
        return seconds
    return min(seconds * 2.0 ** min(failed_passes, 16), max(seconds, _MAX_BACKOFF_SECONDS))


async def _dispatch(client: Client, factory: SummaryRuntimeFactory, tenant_id: str) -> None:
    settings = factory.settings
    await factory.synchronize(tenant_id)
    await factory.control.summaries.reconcile(tenant_id)
    for scope, revision, fence in await factory.control.summaries.runnable_scopes(tenant_id):
        try:
            await client.start_workflow(
                SummaryProjectionWorkflow.run,
                SummaryWorkflowInput(tenant_id, scope, settings.topology_job_seconds),
                id=f"harborrag-summary:{tenant_id}:{scope}:{revision}:{fence}",
                task_queue=settings.summary_task_queue,
                execution_timeout=timedelta(seconds=settings.topology_job_seconds + 3600),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass


async def _poll_delay(seconds: float, stop_event: asyncio.Event | None) -> None:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass
