"""Cached, budget-admitted model decorators for independently retriable views."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.topology.descriptions import DESCRIPTION_MAX_REPAIR_ATTEMPTS
from harborrag_core.contracts import HarborConflictError
from harborrag_core.models.embed import HarborEmbedRequest, HarborEmbedResponse
from harborrag_core.ports.description_generation import UsageAwareDescriptionPort
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol
from harborrag_core.ports.topology import TopologyRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.budget import BudgetRequest, UsageSettlement
from harborrag_core.topology.derived import (
    DescriptionOutput,
    DescriptionPacket,
    description_prompt_json,
)
from harborrag_core.topology.extraction import digest
from harborrag_runtime.tokenization import ApproximateTokenCounter

from .budgeted_extractor import EnrichmentDeferredError
from .contextual import Embedder
from .reservation_deadline import reservation_seconds
from .run_cost import RunCostLedger


@dataclass(frozen=True)
class RequestEmbedder:
    """Adapt the general SDK facade to the narrow typed enrichment port."""

    client: AsyncHarborEmbedClientProtocol

    async def aembed(self, request: HarborEmbedRequest) -> HarborEmbedResponse:
        return await self.client.aembed(request=request)


@dataclass(frozen=True)
class DerivedBudget:
    repository: TopologyRepositoryPort
    tenant_id: str
    build_id: str
    cost_ceiling_usd: Decimal | None

    async def reserve(self, request: BudgetRequest) -> float:
        if self.cost_ceiling_usd is None:
            raise EnrichmentDeferredError("provider_cost_ceiling_unconfigured")
        admission = await self.repository.reserve_for_build(self.tenant_id, self.build_id, request)
        if not admission.admitted:
            raise EnrichmentDeferredError(
                admission.reason or "budget_unavailable",
                retry_after=admission.retry_after,
            )
        try:
            return reservation_seconds(admission)
        except (ValueError, TimeoutError):
            await self.settle(request)
            raise

    async def settle(self, request: BudgetRequest, usage: UsageSettlement | None = None) -> None:
        await asyncio.shield(
            self.repository.settle_budget(
                self.tenant_id,
                request.reservation_id,
                usage or UsageSettlement(),
            )
        )


@dataclass(frozen=True)
class DescriptionArtifacts:
    reader: ImmutableArtifactReader
    writer: ImmutableArtifactWriter
    profile_fingerprint: str


@dataclass(frozen=True)
class FrozenDescriptionGenerator:
    delegate: UsageAwareDescriptionPort
    budget: DerivedBudget
    artifacts: DescriptionArtifacts
    max_output_tokens: int = 500
    # None when the operator set no per-run ceiling.
    run_costs: RunCostLedger | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("description output budget must be positive")

    async def generate(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionOutput:
        identity = digest(
            [
                self.artifacts.profile_fingerprint,
                [packet.model_dump(mode="json") for packet in packets],
            ]
        )
        key = f"topology/descriptions/{self.budget.build_id}/{identity}.json"
        context = StorageOperationContext.system(self.budget.tenant_id)
        existing = await self._read_existing(key, context)
        if existing is not None:
            # Frozen work is reused and costs nothing, so the ceiling does not gate it.
            return existing
        if self.run_costs is not None:
            self.run_costs.ensure_capacity()
        # Includes schema, framing and every bounded repair with its prior response.
        input_tokens = ApproximateTokenCounter().count(description_prompt_json(packets))
        provider_calls = DESCRIPTION_MAX_REPAIR_ATTEMPTS + 1
        reservation = BudgetRequest(
            reservation_id=str(uuid4()),
            purpose="parent_description",
            input_tokens=provider_calls * (input_tokens + 2048),
            output_tokens=provider_calls * self.max_output_tokens,
            cost_usd=self.budget.cost_ceiling_usd or Decimal(0),
            operation_key=identity,
            provider_calls=provider_calls,
        )
        deadline = await self.budget.reserve(reservation)
        # Unknown usage retains the full reservation rather than inventing a discount.
        settlement: UsageSettlement | None = None
        try:
            async with asyncio.timeout(deadline):
                run = await self.delegate.generate_usage(packets)
            output = run.output
            if self.run_costs is not None:
                self.run_costs.record(run.cost_usd)
            reported = run.usage.prompt_tokens + run.usage.completion_tokens
            if reported > 0:
                # cost_usd is None when the deployment declares no pricing, and
                # settle_budget then keeps the reserved charge rather than discounting it.
                settlement = UsageSettlement(
                    input_tokens=run.usage.prompt_tokens,
                    output_tokens=run.usage.completion_tokens,
                    cost_usd=run.cost_usd,
                )
            if not output.complete:
                raise ValueError("description does not account for the supplied scope")
            if not set(output.cited_packet_ids) <= {p.packet_id for p in packets}:
                raise ValueError("description cites an unknown packet")
            try:
                await self.artifacts.writer.put(
                    ImmutableArtifact(
                        bucket=ARTIFACT_BUCKET,
                        key=key,
                        payload=output.model_dump_json().encode(),
                        media_type="application/json",
                        artifact_kind="parent-description-packet",
                    ),
                    context=context,
                )
            except HarborConflictError:
                # Another worker may have frozen the same nondeterministic LLM
                # operation after our initial read. Adopt that verified winner.
                winner = await self._read_existing(key, context)
                if winner is None:
                    raise
                return winner
            return output
        finally:
            await self.budget.settle(reservation, settlement)

    async def _read_existing(
        self,
        key: str,
        context: StorageOperationContext,
    ) -> DescriptionOutput | None:
        existing = await self.artifacts.reader.find(
            bucket=ARTIFACT_BUCKET,
            key=key,
            media_type="application/json",
            context=context,
        )
        if existing is None:
            return None
        payload = await self.artifacts.reader.get(existing, context=context)
        if sha256(payload).hexdigest() != existing.sha256:
            raise ValueError("description artifact integrity failure")
        return DescriptionOutput.model_validate_json(payload)


@dataclass(frozen=True)
class FrozenEmbedder:
    """Checkpoint deterministic projection work outside the LLM spending ledger."""

    delegate: Embedder
    artifacts: DescriptionArtifacts
    tenant_id: str
    build_id: str

    async def aembed(self, request: HarborEmbedRequest) -> HarborEmbedResponse:
        identity = digest([self.artifacts.profile_fingerprint, request.model_dump(mode="json")])
        key = f"topology/embedding-responses/{self.build_id}/{identity}.json"
        context = StorageOperationContext.system(self.tenant_id)
        existing = await self.artifacts.reader.find(
            bucket=ARTIFACT_BUCKET,
            key=key,
            media_type="application/json",
            context=context,
        )
        if existing is not None:
            payload = await self.artifacts.reader.get(existing, context=context)
            if sha256(payload).hexdigest() != existing.sha256:
                raise ValueError("embedding checkpoint integrity failure")
            return HarborEmbedResponse.model_validate_json(payload)
        response = await self.delegate.aembed(request)
        await self.artifacts.writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=key,
                payload=response.model_dump_json().encode(),
                media_type="application/json",
                artifact_kind="derived-embedding-response",
            ),
            context=context,
        )
        return response
