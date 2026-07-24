"""Deterministic activity execution with centrally configured policies."""

from __future__ import annotations

from typing import cast

from temporalio import workflow
from temporalio.workflow import ActivityCancellationType

from harborrag_runtime.temporal.schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ReconciliationInput,
    ReconciliationResult,
    ResolutionDecision,
    ResolutionReceipt,
    WorkflowOptions,
)
from harborrag_runtime.temporal.task_queues import ActivityClass


async def execute_artifact_stage(
    name: str,
    request: ArtifactActivityInput,
    activity_class: ActivityClass,
    options: WorkflowOptions,
) -> ArtifactActivityResult:
    return await execute_activity(
        name,
        request,
        ArtifactActivityResult,
        activity_class,
        options,
    )


async def execute_reconciliation(
    request: ReconciliationInput,
    options: WorkflowOptions,
) -> ReconciliationResult:
    return await execute_activity(
        "harborrag.reconcile_ingestion",
        request,
        ReconciliationResult,
        ActivityClass.MAINTENANCE,
        options,
    )


async def execute_resolution(
    request: ArtifactActivityInput,
    decision: ResolutionDecision,
    options: WorkflowOptions,
) -> ResolutionReceipt:
    policy = options.retries.maintenance
    result = await workflow.execute_activity(
        "harborrag.apply_resolution",
        args=[request, decision],
        task_queue=options.task_queues.maintenance,
        result_type=ResolutionReceipt,
        start_to_close_timeout=policy.start_to_close_timeout,
        heartbeat_timeout=policy.heartbeat_timeout,
        retry_policy=policy.temporal_retry_policy(),
        cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    )
    return cast("ResolutionReceipt", result)


async def execute_activity[ResultT](
    name: str,
    request: object,
    result_type: type[ResultT],
    activity_class: ActivityClass,
    options: WorkflowOptions,
) -> ResultT:
    policy = options.retries.for_class(activity_class)
    result = await workflow.execute_activity(
        name,
        request,
        task_queue=options.task_queues.for_activity(activity_class),
        result_type=result_type,
        start_to_close_timeout=policy.start_to_close_timeout,
        heartbeat_timeout=policy.heartbeat_timeout,
        retry_policy=policy.temporal_retry_policy(),
        cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    )
    return cast("ResultT", result)
