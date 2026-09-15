"""Temporal worker plus transactional-outbox dispatcher for an explicit tenant."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.connection import connect_temporal_client

from .composition import connect_topology_runtime
from .derived_dispatch import DerivedDispatcher
from .service import TopologyEnrichmentService
from .summary_factory import SummaryRuntimeFactory
from .summary_worker import watch_summaries
from .workflow import TopologyEnrichmentWorkflow, TopologyWorkflowInput


class TopologyActivities:
    def __init__(self, service: TopologyEnrichmentService) -> None:
        self._service = service

    @activity.defn(name="harborrag.enrich_topology")
    async def enrich(self, request: TopologyWorkflowInput) -> str:
        result = await self._service.run_once(
            request.tenant_id,
            job_id=request.job_id,
            heartbeat=lambda: activity.heartbeat(request.job_id),
        )
        return result.state


async def dispatch(
    client: Client,
    repository: TopologyRepositoryPort,
    settings: RuntimeSettings,
    tenant_id: str,
) -> int:
    await repository.reconcile(tenant_id)
    jobs = await repository.runnable_jobs(tenant_id, limit=100)
    started = 0
    for job in jobs:
        try:
            await client.start_workflow(
                TopologyEnrichmentWorkflow.run,
                TopologyWorkflowInput(tenant_id, job.job_id, settings.topology_job_seconds),
                id=f"harborrag-topology:{job.job_id}:{job.attempts}",
                task_queue=settings.topology_task_queue,
                execution_timeout=timedelta(seconds=settings.topology_job_seconds + 3600),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
            started += 1
        except WorkflowAlreadyStartedError:
            pass
    return started


async def run_temporal_worker(settings: RuntimeSettings, tenant_id: str) -> None:
    async with connect_topology_runtime(settings) as runtime:
        settings = runtime.settings
        client = await connect_temporal_client(TemporalRuntimeConfig.from_settings(settings))
        activities = TopologyActivities(runtime.service)
        async with Worker(
            client,
            task_queue=settings.topology_task_queue,
            workflows=[TopologyEnrichmentWorkflow],
            activities=[activities.enrich],
            max_concurrent_activities=settings.temporal_max_concurrent_activities,
            graceful_shutdown_timeout=timedelta(
                seconds=settings.temporal_graceful_shutdown_seconds
            ),
        ):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    watch_summaries(
                        client,
                        SummaryRuntimeFactory(
                            settings,
                            runtime.control,
                            runtime.artifact_reader,
                            runtime.artifact_writer,
                        ),
                        tenant_id,
                    )
                )
                tasks.create_task(
                    DerivedDispatcher(runtime.control.topology, runtime.derive, settings).watch(
                        tenant_id
                    )
                )
                while True:
                    await dispatch(client, runtime.control.topology, settings, tenant_id)
                    await asyncio.sleep(settings.topology_poll_seconds)
