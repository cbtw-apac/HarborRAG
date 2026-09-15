"""Durable summary orchestration; large content stays inside activities."""

from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy


@dataclass(frozen=True)
class SummaryWorkflowInput:
    tenant_id: str
    source_scope_id: str
    job_seconds: float = 3600


@workflow.defn(name="harborrag.summary_projection")
class SummaryProjectionWorkflow:
    @workflow.run
    async def run(self, request: SummaryWorkflowInput) -> str:
        return cast(
            str,
            await workflow.execute_activity(
                "harborrag.project_summaries",
                request,
                start_to_close_timeout=timedelta(seconds=request.job_seconds + 30),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=str,
            ),
        )
