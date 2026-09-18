"""Published-content summaries independent of extraction and vector availability."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from harborrag_adapters.topology.descriptions import DESCRIPTION_MAX_REPAIR_ATTEMPTS
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.ports.description_generation import UsageAwareDescriptionPort
from harborrag_core.ports.summary_projection import SummaryExecutionRepositoryPort
from harborrag_core.ports.topology import TopologyBudgetRepositoryPort
from harborrag_core.schemas.ids import TenantId
from harborrag_core.summaries import (
    SummaryBinding,
    SummaryCard,
    SummaryLease,
    SummaryManifest,
    SummarySnapshot,
)
from harborrag_core.topology.budget import BudgetRequest, UsageSettlement
from harborrag_core.topology.derived import (
    DescriptionOutput,
    DescriptionPacket,
    description_prompt_json,
)
from harborrag_core.topology.extraction import digest
from harborrag_engine.topology.summary_planner import SummaryPlanNode
from harborrag_engine.topology.summary_reducer import (
    SummaryBudgetDeferred,
    SummaryReducer,
    input_digest,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.tokenization import ApproximateTokenCounter

from .budgeted_extractor import EnrichmentDeferredError
from .reservation_deadline import reservation_seconds

logger = logging.getLogger("harborrag.runtime.topology")
type SummaryPlanLoader = Callable[
    [SummaryLease, SummarySnapshot], Awaitable[tuple[SummaryPlanNode, ...]]
]


@dataclass
class BudgetedSummaryGenerator:
    delegate: UsageAwareDescriptionPort
    repository: SummaryExecutionRepositoryPort
    budget: TopologyBudgetRepositoryPort
    lease: SummaryLease
    settings: RuntimeSettings
    spent_usd: Decimal = Decimal(0)

    async def generate(self, packets: tuple[DescriptionPacket, ...]) -> DescriptionOutput:
        ceiling = self.settings.topology_llm_operation_cost_usd
        if ceiling is None:
            raise SummaryBudgetDeferred("provider_cost_ceiling_unconfigured")
        run_ceiling = self.settings.topology_parent_run_budget_usd
        if run_ceiling is not None and self.spent_usd + ceiling > run_ceiling:
            raise SummaryBudgetDeferred("summary_run_budget")
        calls = DESCRIPTION_MAX_REPAIR_ATTEMPTS + 1
        request = BudgetRequest(
            reservation_id=uuid4().hex,
            purpose="parent_description",
            input_tokens=calls
            * (ApproximateTokenCounter().count(description_prompt_json(packets)) + 2048),
            output_tokens=calls * self.settings.topology_parent_max_output_tokens,
            cost_usd=ceiling,
            operation_key=digest([self.lease.policy.fingerprint, description_prompt_json(packets)]),
            provider_calls=calls,
        )
        admission = await self.repository.reserve(self.lease, request)
        if not admission.admitted:
            raise SummaryBudgetDeferred(admission.reason or "budget_unavailable")
        usage = UsageSettlement()
        try:
            async with asyncio.timeout(reservation_seconds(admission)):
                result = await self.delegate.generate_usage(packets)
            self.spent_usd += result.cost_usd if result.cost_usd is not None else ceiling
            usage = UsageSettlement(
                input_tokens=result.usage.prompt_tokens or None,
                output_tokens=result.usage.completion_tokens or None,
                cost_usd=result.cost_usd,
            )
            return result.output
        finally:
            await asyncio.shield(
                self.budget.settle_budget(self.lease.tenant_id, request.reservation_id, usage)
            )


@dataclass(frozen=True)
class SummaryProjectionService:
    repository: SummaryExecutionRepositoryPort
    budget: TopologyBudgetRepositoryPort
    settings: RuntimeSettings
    generator_factory: Callable[[SummaryLease], UsageAwareDescriptionPort]
    load_inputs: SummaryPlanLoader

    async def run_once(
        self,
        tenant_id: str,
        *,
        source_scope_id: str | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> str:
        lease = await self.repository.claim(
            tenant_id,
            lease_seconds=self.settings.topology_lease_seconds,
            source_scope_id=source_scope_id,
        )
        if lease is None:
            return "idle"
        try:
            async with asyncio.timeout(self.settings.topology_job_seconds):
                async with asyncio.TaskGroup() as tasks:
                    renewal = tasks.create_task(self._renew(lease, heartbeat))
                    try:
                        await self._complete(lease)
                    finally:
                        renewal.cancel()
            current = await self.repository.finish(lease)
            return "current" if current else "superseded"
        except asyncio.CancelledError:
            # Lease expiry allows another worker to reuse completed reductions.
            raise
        except Exception as error:
            blocked, code = _summary_failure(error)
            details = {
                "tenant_id": lease.tenant_id,
                "source_scope_id": lease.source_scope_id,
                "error_code": code,
                "blocked": blocked,
            }
            if blocked:
                logger.info("Summary projection blocked", extra=details)
            else:
                logger.warning("Summary projection attempt failed", exc_info=True, extra=details)
            await self.repository.finish(lease, error_code=code[:128], blocked=blocked)
            return "blocked" if blocked else "failed"

    async def _renew(self, lease: SummaryLease, heartbeat: Callable[[], None] | None) -> None:
        while True:
            if heartbeat:
                heartbeat()
            await asyncio.sleep(min(20, self.settings.topology_lease_seconds / 3))
            await self.repository.renew(lease, lease_seconds=self.settings.topology_lease_seconds)

    async def _complete(self, lease: SummaryLease) -> None:
        if lease.source_scope_id == "@tenant":
            await self._complete_tenant(lease)
            return
        repository = self.repository
        snapshot = await repository.snapshot(lease)
        plan = await self.load_inputs(lease, snapshot)
        reducer = SummaryReducer(
            lease.tenant_id,
            lease.policy,
            repository,
            BudgetedSummaryGenerator(
                self.generator_factory(lease), repository, self.budget, lease, self.settings
            ),
        )
        cards: dict[str, SummaryCard] = {}
        by_key = {item.node.node_key: item.node for item in plan}
        for item in plan:
            if item.kind not in {
                "Structure",
                "DocumentVersion",
            } and not await repository.scope_current(lease):
                continue
            metadata: dict[str, object] = {
                "name": item.node.title or item.node.entity_type.value,
                "type": item.node.entity_type.value,
                "path": item.node.section_path,
            }
            inputs = (
                *tuple(chunk.content for chunk in item.direct_chunks if chunk.content.strip()),
                *(
                    json.dumps(
                        {
                            "name": by_key[key].title,
                            "type": by_key[key].entity_type.value,
                            "path": by_key[key].section_path,
                            "card": cards[key].model_dump(mode="json"),
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    for key in item.children
                ),
            )
            card, key = await reducer.reduce(item.kind, metadata, inputs)
            versions = {
                key: snapshot.document_versions[key]
                for key in item.document_ids
                if key in snapshot.document_versions
            }
            dependencies = tuple(
                dep
                for dep in snapshot.permission_dependencies
                if dep.resource_kind == "source" or dep.resource_id in versions
            )
            binding = SummaryBinding(
                manifest=SummaryManifest(
                    node_key=item.node.node_key,
                    kind=item.kind,
                    source_scope_id=lease.source_scope_id,
                    input_document_versions=versions,
                    permission_dependencies=dependencies,
                    child_keys=item.children,
                    child_artifact_hashes={key: cards[key].artifact_hash for key in item.children},
                    input_chunk_ids=item.input_chunk_ids,
                    policy_fingerprint=lease.policy.fingerprint,
                    membership_digest=snapshot.membership_digest,
                    input_digest=input_digest(metadata, inputs),
                ),
                card=card,
                generation_key=key,
                artifact_hash=card.artifact_hash,
                revision=lease.revision,
                updated_at=utc_now(),
                coverage_mode="complete" if item.input_chunk_ids else "empty",
            )
            await repository.accept(lease, snapshot, binding, item.node)
            cards[item.node.node_key] = card

    async def _complete_tenant(self, lease: SummaryLease) -> None:
        repository = self.repository
        snapshot = await repository.snapshot(lease)
        children = await repository.tenant_inputs(lease)
        node = GraphNodeRecord(
            node_key=DocumentIdentityBuilder().tenant_node_key(tenant_id=lease.tenant_id),
            node_kind=KnowledgeNodeKind.TENANT,
            entity_type=GraphEntityType.TENANT,
            logical_id=lease.tenant_id,
            ownership_scope=GraphOwnershipScope.TENANT,
            owner_id=TenantId(lease.tenant_id),
            title=lease.tenant_id,
        )
        reducer = SummaryReducer(
            lease.tenant_id,
            lease.policy,
            repository,
            BudgetedSummaryGenerator(
                self.generator_factory(lease), repository, self.budget, lease, self.settings
            ),
        )
        inputs = tuple(child.card.model_dump_json() for child in children)
        metadata: dict[str, object] = {"name": lease.tenant_id, "type": "tenant"}
        card, key = await reducer.reduce("Tenant", metadata, inputs)
        chunk_ids = tuple(
            sorted({key for child in children for key in child.manifest.input_chunk_ids})
        )
        binding = SummaryBinding(
            manifest=SummaryManifest(
                node_key=node.node_key,
                kind="Tenant",
                source_scope_id="@tenant",
                input_document_versions=snapshot.document_versions,
                permission_dependencies=snapshot.permission_dependencies,
                child_keys=tuple(child.manifest.node_key for child in children),
                child_artifact_hashes={
                    child.manifest.node_key: child.artifact_hash for child in children
                },
                input_chunk_ids=chunk_ids,
                policy_fingerprint=lease.policy.fingerprint,
                membership_digest=snapshot.membership_digest,
                input_digest=input_digest(metadata, inputs),
            ),
            card=card,
            generation_key=key,
            artifact_hash=card.artifact_hash,
            revision=lease.revision,
            updated_at=utc_now(),
            coverage_mode="complete" if chunk_ids else "empty",
        )
        await repository.accept(lease, snapshot, binding, node)


def _summary_failure(error: BaseException) -> tuple[bool, str]:
    """Classify every concurrent failure; infrastructure errors take precedence."""

    leaves = _exception_leaves(error)
    blocked_types = (SummaryBudgetDeferred, EnrichmentDeferredError, HarborConflictError)
    failures = tuple(cause for cause in leaves if not isinstance(cause, blocked_types))
    cause = failures[0] if failures else leaves[0]
    blocked = not failures
    code = (
        str(cause)
        if isinstance(cause, (SummaryBudgetDeferred, HarborConflictError))
        else type(cause).__name__
    )
    return blocked, code


def _exception_leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in _exception_leaves(child))
    return (error,)
