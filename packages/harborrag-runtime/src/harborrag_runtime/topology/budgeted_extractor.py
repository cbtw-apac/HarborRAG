"""Admission decorator: reserve before dispatch, conservatively charge unknown usage."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from harborrag_adapters.topology.extraction_budget import (
    MAX_PROVIDER_CALLS,
    extraction_operation_key,
)
from harborrag_adapters.topology.extractor import extraction_request_budget
from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_core.ports.topology_extraction import UsageAwareExtractionPort
from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractionOutput,
    ExtractionProfile,
    TopologyJob,
    digest,
)
from harborrag_core.topology.budget import BudgetRequest, UsageSettlement

from .reservation_deadline import reservation_seconds


class EnrichmentDeferredError(RuntimeError):
    """No provider dispatch occurred because the durable spending policy deferred it."""

    def __init__(self, reason: str, *, retry_after: datetime | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def extraction_attempt_operation_key(
    value: ChunkExtractionInput,
    profile: ExtractionProfile,
    *,
    attempt: int,
) -> str:
    """Deduplicate replay within an attempt while allowing bounded job retries."""

    if attempt < 1:
        raise ValueError("claimed topology jobs require a positive attempt")
    return digest([extraction_operation_key(value, profile), attempt])


@dataclass(frozen=True)
class BudgetedExtractor:
    delegate: UsageAwareExtractionPort
    repository: TopologyRepositoryPort
    job: TopologyJob
    cost_ceiling_usd: Decimal | None

    async def extract(
        self,
        value: ChunkExtractionInput,
        *,
        profile: ExtractionProfile,
        tenant_id: str,
        document_id: str,
    ) -> ExtractionOutput:
        if tenant_id != self.job.tenant_id or document_id != self.job.document_id:
            raise ValueError("budgeted extraction scope differs from claimed work")
        if self.cost_ceiling_usd is None:
            raise EnrichmentDeferredError("provider_cost_ceiling_unconfigured")
        inputs, outputs = extraction_request_budget(value, profile)
        request = BudgetRequest(
            reservation_id=str(uuid4()),
            input_tokens=inputs,
            output_tokens=outputs,
            cost_usd=self.cost_ceiling_usd,
            operation_key=extraction_attempt_operation_key(
                value, profile, attempt=self.job.attempts
            ),
            provider_calls=MAX_PROVIDER_CALLS,
            max_provider_calls=MAX_PROVIDER_CALLS,
        )
        admission = await self.repository.reserve_budget(self.job, request)
        if not admission.admitted:
            raise EnrichmentDeferredError(
                admission.reason or "budget_unavailable",
                retry_after=admission.retry_after,
            )
        # Unknown usage retains the full reservation. Failure and cancellation never
        # reassign it downwards, because no dispatch reported what it actually cost.
        settlement = UsageSettlement()
        try:
            async with asyncio.timeout(reservation_seconds(admission)):
                run = await self.delegate.extract_usage(
                    value, profile=profile, tenant_id=tenant_id, document_id=document_id
                )
                reported = run.usage.prompt_tokens + run.usage.completion_tokens
                if reported > 0:
                    # A provider that omits usage must not settle to zero, which would
                    # make the call free against the daily cap.
                    settlement = UsageSettlement(
                        input_tokens=run.usage.prompt_tokens,
                        output_tokens=run.usage.completion_tokens,
                    )
                return run.output
        finally:
            await asyncio.shield(
                self.repository.settle_budget(tenant_id, request.reservation_id, settlement)
            )
