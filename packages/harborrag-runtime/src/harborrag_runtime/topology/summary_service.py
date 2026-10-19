"""Published-content summaries independent of extraction and vector availability."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from harborrag_adapters.topology.descriptions import (
    DESCRIPTION_MAX_REPAIR_ATTEMPTS,
    description_max_chars,
    description_output_tokens,
)
from harborrag_core.base import utc_now
from harborrag_core.chunking import ChunkRecord
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
    LEAF_SUMMARY_KINDS,
    MissingSourceDocument,
    SummaryAttribute,
    SummaryBinding,
    SummaryCard,
    SummaryCoverage,
    SummaryLease,
    SummaryManifest,
    SummaryPolicy,
    SummarySnapshot,
    generation_key,
)
from harborrag_core.topology.budget import BudgetRequest, UsageSettlement
from harborrag_core.topology.derived import (
    DescriptionOutput,
    DescriptionPacket,
    description_prompt_json,
)
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.text_policy import PARENT_DESCRIPTION_MAX_WORDS
from harborrag_engine.topology.summary_planner import SummaryPlanNode
from harborrag_engine.topology.summary_reducer import (
    SummaryBudgetDeferred,
    SummaryReducer,
    input_digest,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.tokenization import ApproximateTokenCounter

from .budgeted_extractor import EnrichmentDeferredError
from .entity_summary_index import EntitySummaryIndex
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

    async def generate(
        self,
        packets: tuple[DescriptionPacket, ...],
        *,
        max_words: int = PARENT_DESCRIPTION_MAX_WORDS,
    ) -> DescriptionOutput:
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
            # A dossier is allowed more words than a navigation card, so the
            # reservation has to cover them or a wide budget would be admitted
            # against a narrow one's estimate.
            output_tokens=calls * self._output_tokens(max_words),
            cost_usd=ceiling,
            operation_key=digest(
                [
                    self.lease.policy.fingerprint,
                    description_prompt_json(packets),
                    max_words,
                ]
            ),
            provider_calls=calls,
        )
        admission = await self.repository.reserve(self.lease, request)
        if not admission.admitted:
            raise SummaryBudgetDeferred(admission.reason or "budget_unavailable")
        usage = UsageSettlement()
        try:
            async with asyncio.timeout(reservation_seconds(admission)):
                result = await self.delegate.generate_usage(packets, max_words=max_words)
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

    def _output_tokens(self, max_words: int) -> int:
        """Reserve exactly what the request will be allowed to spend."""

        return description_output_tokens(max_words, self.settings.topology_parent_max_output_tokens)


@dataclass(frozen=True)
class SummaryProjectionService:
    repository: SummaryExecutionRepositoryPort
    budget: TopologyBudgetRepositoryPort
    settings: RuntimeSettings
    generator_factory: Callable[[SummaryLease], UsageAwareDescriptionPort]
    load_inputs: SummaryPlanLoader
    # Absent when the deployment has no vector backend wired into the summary
    # worker; cards are still written and still readable, only not searchable.
    entity_index: EntitySummaryIndex | None = None

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
        coverage = _CoverageLedger.of(
            await repository.missing_documents(lease.tenant_id, lease.source_scope_id)
        )
        run = _Run(
            lease=lease,
            snapshot=snapshot,
            coverage=coverage,
            plans={item.node.node_key: item for item in plan},
            reducer=SummaryReducer(
                lease.tenant_id,
                lease.policy,
                repository,
                BudgetedSummaryGenerator(
                    self.generator_factory(lease), repository, self.budget, lease, self.settings
                ),
                # ``max_calls`` is configured per document; a scope run covers them
                # all. Spend is still bounded by the tenant's daily token/cost caps.
                max_calls=lease.policy.max_calls * max(1, len(snapshot.document_versions)),
            ),
            semaphore=asyncio.Semaphore(lease.policy.max_concurrency),
        )
        # Cards are generated in dependency waves: every section, then every
        # document, then the entities. Within a wave nothing depends on anything
        # else, so the tenant's card concurrency applies -- the old one-at-a-time
        # walk spent hours on a scope that needs minutes.
        for wave in _waves(plan):
            if any(item.kind not in LEAF_SUMMARY_KINDS for item in wave):
                # Entity cards compose every document under the entity. While the
                # scope is still being ingested each one would be superseded by the
                # next publish, so leaves are built now and entities wait.
                if await repository.ingestion_active(lease.tenant_id, lease.source_scope_id):
                    raise HarborConflictError("SUMMARY_INGESTION_ACTIVE")
                if not await repository.scope_current(lease):
                    run.complete = False
                    break
            async with asyncio.TaskGroup() as tasks:
                for item in wave:
                    tasks.create_task(self._bounded_card(run, item))
        if run.complete:
            await self._publish_entities(lease, tuple(run.entities))

    async def _bounded_card(self, run: _Run, item: SummaryPlanNode) -> None:
        """One card at a time per semaphore slot -- the model call *and* the accept.

        Bounding only the model call let a wave of a few hundred shortcut cards open
        a database session each for ``accept`` at once and exhaust the control-plane
        pool. The limit is on cards in flight, which is also what the budget means.
        """

        async with run.semaphore:
            await self._card(run, item)

    async def _card(self, run: _Run, item: SummaryPlanNode) -> None:
        """Produce and accept one node's card."""

        lease, policy = run.lease, run.lease.policy
        direct = tuple(chunk for chunk in item.direct_chunks if chunk.content.strip())
        metadata: dict[str, object] = {
            "name": item.node.title or item.node.entity_type.value,
            "type": item.node.entity_type.value,
            "path": item.node.section_path,
        }
        inputs = (
            *tuple(chunk.content for chunk in direct),
            *(
                json.dumps(
                    {
                        "name": run.plans[key].node.title,
                        "type": run.plans[key].node.entity_type.value,
                        "path": run.plans[key].node.section_path,
                        "card": run.cards[key].model_dump(mode="json"),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for key in item.children
            ),
        )
        shortcut = _shortcut(lease.tenant_id, policy, item, direct, run.cards)
        if shortcut is not None:
            card, key = shortcut
        else:
            card, key = await run.reducer.reduce(item.kind, metadata, inputs)
        if item.kind == "SourceEntity":
            # The entity's own document is the one its direct chunks belong to;
            # attachments hang beneath it as their own entities.
            own = tuple(dict.fromkeys(str(chunk.document_id) for chunk in direct))
            card = _with_facets(card, item.node, policy, own or item.document_ids)
        versions = {
            key: run.snapshot.document_versions[key]
            for key in item.document_ids
            if key in run.snapshot.document_versions
        }
        dependencies = tuple(
            dep
            for dep in run.snapshot.permission_dependencies
            if dep.resource_kind == "source" or dep.resource_id in versions
        )
        missing = run.coverage.missing_for(item.kind, item.document_ids)
        binding = SummaryBinding(
            manifest=SummaryManifest(
                node_key=item.node.node_key,
                kind=item.kind,
                source_scope_id=lease.source_scope_id,
                input_document_versions=versions,
                permission_dependencies=dependencies,
                child_keys=item.children,
                child_artifact_hashes={key: run.cards[key].artifact_hash for key in item.children},
                input_chunk_ids=item.input_chunk_ids,
                missing_document_ids=missing,
                policy_fingerprint=policy.fingerprint,
                membership_digest=run.snapshot.membership_digest,
                input_digest=input_digest(metadata, inputs),
            ),
            card=card,
            generation_key=key,
            artifact_hash=card.artifact_hash,
            revision=lease.revision,
            updated_at=utc_now(),
            coverage_mode=_coverage_mode(item.input_chunk_ids, missing),
        )
        await self.repository.accept(lease, run.snapshot, binding, item.node)
        run.cards[item.node.node_key] = card
        if item.kind == "SourceEntity":
            run.entities.append(binding)

    async def _publish_entities(
        self, lease: SummaryLease, bindings: tuple[SummaryBinding, ...]
    ) -> None:
        """Make this scope's entity cards searchable, and only this scope's.

        A failure here must not fail the run: the cards are already accepted and
        readable, and the index is rebuilt from them on the next pass. Losing a
        search entry point is a degraded result; losing the binding is not.
        """

        if self.entity_index is None or not await self.repository.scope_current(lease):
            return
        try:
            await self.entity_index.publish(
                lease.tenant_id, lease.source_scope_id, bindings, lease.policy.facets
            )
        except Exception:
            logger.warning(
                "Entity summary index publication failed",
                exc_info=True,
                extra={
                    "tenant_id": lease.tenant_id,
                    "source_scope_id": lease.source_scope_id,
                    "entities": len(bindings),
                },
            )

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
        missing = tuple(
            sorted({key for child in children for key in child.manifest.missing_document_ids})
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
                missing_document_ids=missing,
                policy_fingerprint=lease.policy.fingerprint,
                membership_digest=snapshot.membership_digest,
                input_digest=input_digest(metadata, inputs),
            ),
            card=card,
            generation_key=key,
            artifact_hash=card.artifact_hash,
            revision=lease.revision,
            updated_at=utc_now(),
            coverage_mode=_coverage_mode(chunk_ids, missing),
        )
        await repository.accept(lease, snapshot, binding, node)


@dataclass
class _Run:
    """Everything one scope run shares across its concurrently generated cards."""

    lease: SummaryLease
    snapshot: SummarySnapshot
    coverage: _CoverageLedger
    plans: dict[str, SummaryPlanNode]
    reducer: SummaryReducer
    semaphore: asyncio.Semaphore
    cards: dict[str, SummaryCard] = field(default_factory=dict)
    entities: list[SummaryBinding] = field(default_factory=list)
    complete: bool = True


def _waves(plan: tuple[SummaryPlanNode, ...]) -> tuple[tuple[SummaryPlanNode, ...], ...]:
    """Group the plan by dependency depth; every node's children are in an earlier wave."""

    depth: dict[str, int] = {}
    for item in plan:  # the planner emits children before parents
        depth[item.node.node_key] = 1 + max(
            (depth[child] for child in item.children if child in depth), default=-1
        )
    waves: dict[int, list[SummaryPlanNode]] = {}
    for item in plan:
        waves.setdefault(depth[item.node.node_key], []).append(item)
    return tuple(tuple(waves[level]) for level in sorted(waves))


def _shortcut(
    tenant_id: str,
    policy: SummaryPolicy,
    item: SummaryPlanNode,
    direct: tuple[ChunkRecord, ...],
    cards: dict[str, SummaryCard],
) -> tuple[SummaryCard, str] | None:
    """A card that needs no model: the content already fits, or there is one child.

    A section holding one 250-token comment does not need a model to say what it
    says; neither does an attachment entity whose only child is the document card
    written a moment earlier. Both were a model call each -- together well over
    half of every run -- and the model's answer was a paraphrase of the input.
    """

    words = policy.card_words.for_kind(item.kind)
    if not direct and len(item.children) == 1:
        child = cards[item.children[0]]
        return child, generation_key(
            tenant_id, policy, ["inherit", item.node.node_key, child.artifact_hash, words]
        )
    if item.kind not in LEAF_SUMMARY_KINDS:
        return None
    texts = [
        *(chunk.content.strip() for chunk in direct),
        *(cards[key].description.strip() for key in item.children),
    ]
    joined = "\n\n".join(text for text in texts if text)
    if not joined or len(joined.split()) > words or len(joined) > description_max_chars(words):
        return None
    # A verbatim card of children that carry facets keeps their union; leaves have
    # none today, but the rule is what makes the shortcut safe to widen later.
    return SummaryCard(description=joined), generation_key(
        tenant_id, policy, ["verbatim", texts, words], fingerprint=policy.leaf_fingerprint
    )


def _with_facets(
    card: SummaryCard,
    node: GraphNodeRecord,
    policy: SummaryPolicy,
    document_ids: tuple[str, ...] = (),
) -> SummaryCard:
    """Copy the connector's structured fields onto the card as its facets.

    A Jira *Stage* field is the recruiter's own statement, so it is copied as-is
    rather than read back out of the content by a model. ``document_ids`` is the
    provenance to record: a source entity node carries no document id of its own,
    so the caller names the document(s) the fields came from.
    """

    if not policy.facets:
        return card
    fields = _node_fields(node)
    copied: dict[str, SummaryAttribute] = {}
    for facet in policy.facets:
        value = fields.get(facet.field.strip().casefold())
        if not value:
            continue
        copied[facet.name] = SummaryAttribute(
            name=facet.name,
            values=(value[:200],),
            from_document_ids=tuple(sorted(document_ids))[:64],
        )
    if not copied:
        return card
    return card.model_copy(update={"attributes": tuple(copied.values())})


def _node_fields(node: GraphNodeRecord) -> dict[str, str]:
    """Every field a facet may name on this node, keyed case-insensitively.

    Standard issue attributes come first (``status``, ``priority``, ``labels``, ...):
    the pipeline stage of a candidate is the issue status, not a custom field. A
    custom field answers to its field id and to its display name alike.
    """

    fields: dict[str, str] = {}
    for key, value in node.attributes.items():
        if key == "custom_fields" or value in (None, "", [], {}):
            continue
        rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        fields[str(key).strip().casefold()] = rendered
    rows = node.attributes.get("custom_fields")
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not row.get("value"):
            continue
        for key in ("field_id", "name"):
            if row.get(key):
                fields[str(row[key]).strip().casefold()] = str(row["value"])
    return fields


def _coverage_mode(chunks: tuple[str, ...], missing: tuple[str, ...]) -> SummaryCoverage:
    """Say which of the three things a card is: written over everything, over
    part of it, or over nothing."""

    if missing:
        return "partial"
    return "complete" if chunks else "empty"


@dataclass(frozen=True)
class _CoverageLedger:
    """Attribute each undelivered source item to the nodes that should contain it.

    An attachment still in OCR, or one whose file type nothing can parse, is
    discovered long before it is parsed. Reading the gap from discovery rather
    than from what published is what lets a card say it is partial instead of
    silently describing a candidate without their CV.
    """

    by_parent: dict[str, tuple[str, ...]]
    orphans: tuple[str, ...]

    @classmethod
    def of(cls, missing: tuple[MissingSourceDocument, ...]) -> _CoverageLedger:
        identifiers = {item.document_id for item in missing}
        by_parent: dict[str, tuple[str, ...]] = {}
        orphans: tuple[str, ...] = ()
        for item in missing:
            parent = item.parent_document_id
            # A child of an absent parent has no node to belong to either, so it
            # rises to the source level rather than disappearing from the count.
            if parent is None or parent in identifiers:
                orphans += (item.document_id,)
                continue
            by_parent[parent] = by_parent.get(parent, ()) + (item.document_id,)
        return cls(by_parent, orphans)

    def missing_for(self, kind: str, documents: tuple[str, ...]) -> tuple[str, ...]:
        found = {item for parent in documents for item in self.by_parent.get(parent, ())}
        if kind in {"DataSource", "Tenant"}:
            found.update(self.orphans)
        return tuple(sorted(found))


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
