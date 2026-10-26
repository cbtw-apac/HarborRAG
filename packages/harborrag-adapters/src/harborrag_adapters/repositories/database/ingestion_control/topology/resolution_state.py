"""Immutable resolution snapshots, rebuilt from explicit non-reverted decisions."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import ResolutionDecision, ResolutionRequest

from .schema import TOPOLOGY_RESOLUTION_HEADS


async def lock_resolution_head(session: AsyncSession, tenant_id: str) -> int:
    factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    await session.execute(
        factory(TOPOLOGY_RESOLUTION_HEADS)
        .values(
            tenant_id=tenant_id,
            revision=0,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    value = (
        await session.execute(
            select(TOPOLOGY_RESOLUTION_HEADS.c.revision)
            .where(
                TOPOLOGY_RESOLUTION_HEADS.c.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one()
    return int(value)


def validate_reversal(request: ResolutionRequest, history: Sequence[ResolutionDecision]) -> None:
    if request.action != "revert":
        return
    previous = next(
        (item for item in history if item.request.decision_id == request.reverts_decision_id), None
    )
    reverted = {
        item.request.reverts_decision_id for item in history if item.request.action == "revert"
    }
    if previous is None or previous.request.action != "merge":
        raise HarborConflictError("only an existing tenant merge decision can be reverted")
    if request.reverts_decision_id in reverted:
        raise HarborConflictError("merge decision was already reverted")


def resolution_mapping(history: Sequence[ResolutionDecision]) -> dict[str, str]:
    reverted = {
        item.request.reverts_decision_id for item in history if item.request.action == "revert"
    }
    parent: dict[str, str] = {}

    def root(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            value = parent[value]
        return value

    for decision in history:
        request = decision.request
        if request.action == "revert":
            continue
        if request.action != "merge":
            # A new action added to the contract but not to this replay would otherwise
            # log a decision, bump every tenant policy and rebuild the whole corpus while
            # leaving the mapping byte-identical -- a no-op that looks like it worked.
            raise HarborConflictError(
                f"resolution replay does not handle action {request.action!r}"
            )
        if request.decision_id in reverted:
            continue
        roots = sorted({root(value) for value in request.entity_ids})
        winner = roots[0]
        for value in roots[1:]:
            parent[value] = winner
    if len(parent) > 1000:
        raise HarborConflictError("manual entity resolution exceeds the 1000-entity tenant budget")
    return {value: root(value) for value in sorted(parent)}
