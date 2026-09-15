"""Independent enrichment workflow: existing document histories are unchanged."""

from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy


@dataclass(frozen=True)
class TopologyWorkflowInput:
    tenant_id: str
    job_id: str
    job_seconds: float = 3600


@workflow.defn(name="harborrag.topology_enrichment")
class TopologyEnrichmentWorkflow:
    @workflow.run
    async def run(self, request: TopologyWorkflowInput) -> str:
        return cast(
            str,
            await workflow.execute_activity(
                "harborrag.enrich_topology",
                request,
                start_to_close_timeout=timedelta(seconds=request.job_seconds + 30),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=str,
            ),
        )
