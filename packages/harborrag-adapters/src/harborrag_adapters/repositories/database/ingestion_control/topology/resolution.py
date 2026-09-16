"""Explicit merge/revert decisions invalidate serving builds without changing evidence."""

from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import (
    CanonicalMention,
    ResolutionDecision,
    ResolutionRequest,
    TopologyJob,
    TopologyPolicy,
)

from .guards import require_lease
from .identity_proofs import resolution_proofs
from .resolution_state import lock_resolution_head, resolution_mapping, validate_reversal
from .schema import (
    TOPOLOGY_MENTIONS,
    TOPOLOGY_POLICIES,
    TOPOLOGY_RESOLUTION_DECISIONS,
    TOPOLOGY_RESOLUTION_HEADS,
    TOPOLOGY_RESOLUTION_SNAPSHOTS,
)
from .transactions import topology_transaction


async def validate_merge_entities(session: AsyncSession, request: ResolutionRequest) -> None:
    if request.action != "merge":
        return
    records = (
        (
            await session.execute(
                select(TOPOLOGY_MENTIONS.c.record).where(
                    TOPOLOGY_MENTIONS.c.tenant_id == request.tenant_id,
                    TOPOLOGY_MENTIONS.c.entity_id.in_(request.entity_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    mentions = tuple(CanonicalMention.model_validate(value) for value in records)
    if {value.entity_id for value in mentions} != set(request.entity_ids):
        raise HarborConflictError(
            "merge entities must already have canonical mentions in this tenant"
        )
    if len({value.observation.entity_type.casefold() for value in mentions}) != 1:
        raise HarborConflictError("manual merge requires matching entity types")


async def advance_resolution_policies(session: AsyncSession, tenant_id: str, revision: str) -> None:
    policies = (
        (
            await session.execute(
                select(TOPOLOGY_POLICIES)
                .where(
                    TOPOLOGY_POLICIES.c.tenant_id == tenant_id,
                )
                .order_by(TOPOLOGY_POLICIES.c.source_scope_id)
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    for row in policies:
        policy = TopologyPolicy.model_validate(row["policy"]).model_copy(
            update={"resolution_revision": revision}
        )
        await session.execute(
            update(TOPOLOGY_POLICIES)
            .where(
                TOPOLOGY_POLICIES.c.tenant_id == tenant_id,
                TOPOLOGY_POLICIES.c.source_scope_id == policy.source_scope_id,
            )
            .values(
                revision=row["revision"] + 1,
                policy=policy.model_dump(mode="json"),
                fingerprint=policy.fingerprint,
            )
        )


class TopologyResolutionOperations:
    _client: SQLAlchemyDBClient

    async def record_resolution(self, request: ResolutionRequest) -> ResolutionDecision:
        async with topology_transaction(self._client) as session:
            previous_revision = await lock_resolution_head(session, request.tenant_id)
            values = (
                (
                    await session.execute(
                        select(TOPOLOGY_RESOLUTION_DECISIONS.c.decision)
                        .where(
                            TOPOLOGY_RESOLUTION_DECISIONS.c.tenant_id == request.tenant_id,
                        )
                        .order_by(TOPOLOGY_RESOLUTION_DECISIONS.c.revision)
                    )
                )
                .scalars()
                .all()
            )
            history = [ResolutionDecision.model_validate(value) for value in values]
            existing = next(
                (value for value in history if value.request.decision_id == request.decision_id),
                None,
            )
            if existing is not None:
                if existing.request != request:
                    raise HarborConflictError("resolution decision ID is immutable")
                return existing
            if len(history) >= 1000:
                raise HarborConflictError(
                    "manual resolution exceeds the 1000-decision tenant budget"
                )
            await validate_merge_entities(session, request)
            validate_reversal(request, history)
            revision = previous_revision + 1
            decision = ResolutionDecision(
                request=request,
                revision=revision,
                created_at=utc_now(),
                resolution_revision=f"manual:{revision}",
            )
            mapping = resolution_mapping([*history, decision])
            proofs = await resolution_proofs(session, request.tenant_id, mapping)
            await session.execute(
                insert(TOPOLOGY_RESOLUTION_DECISIONS).values(
                    tenant_id=request.tenant_id,
                    decision_id=request.decision_id,
                    revision=revision,
                    decision=decision.model_dump(mode="json"),
                )
            )
            await session.execute(
                insert(TOPOLOGY_RESOLUTION_SNAPSHOTS).values(
                    tenant_id=request.tenant_id,
                    resolution_revision=decision.resolution_revision,
                    mapping=mapping,
                    proofs=proofs,
                )
            )
            await session.execute(
                update(TOPOLOGY_RESOLUTION_HEADS)
                .where(
                    TOPOLOGY_RESOLUTION_HEADS.c.tenant_id == request.tenant_id,
                )
                .values(revision=revision)
            )
            await advance_resolution_policies(
                session, request.tenant_id, decision.resolution_revision
            )
        return decision

    async def list_resolutions(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[ResolutionDecision, ...]:
        async with self._client.sessions() as session:
            values = (
                (
                    await session.execute(
                        select(TOPOLOGY_RESOLUTION_DECISIONS.c.decision)
                        .where(
                            TOPOLOGY_RESOLUTION_DECISIONS.c.tenant_id == tenant_id,
                        )
                        .order_by(TOPOLOGY_RESOLUTION_DECISIONS.c.revision.desc())
                        .limit(max(1, min(limit, 1000)))
                    )
                )
                .scalars()
                .all()
            )
        return tuple(ResolutionDecision.model_validate(value) for value in values)

    async def resolve_entities(
        self, job: TopologyJob, entity_ids: tuple[str, ...]
    ) -> dict[str, str]:
        if len(entity_ids) > 100000:
            raise ValueError("entity resolution input exceeds the document entity budget")
        async with topology_transaction(self._client) as session:
            await require_lease(session, job)
            revision = job.policy.resolution_revision
            if revision == "conservative-v1":
                return {value: value for value in entity_ids}
            snapshot = (
                (
                    await session.execute(
                        select(TOPOLOGY_RESOLUTION_SNAPSHOTS).where(
                            TOPOLOGY_RESOLUTION_SNAPSHOTS.c.tenant_id == job.tenant_id,
                            TOPOLOGY_RESOLUTION_SNAPSHOTS.c.resolution_revision == revision,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if snapshot is None:
                raise HarborConflictError("topology resolution snapshot does not exist")
        mapping = snapshot["mapping"] if snapshot["proofs"] else {}
        return {value: str(mapping.get(value, value)) for value in entity_ids}
