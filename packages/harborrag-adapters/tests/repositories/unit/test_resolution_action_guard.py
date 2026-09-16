"""An unhandled resolution action must fail loudly, not replay as a silent no-op."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_adapters.repositories.database.ingestion_control.topology.resolution_state import (
    resolution_mapping,
)
from harborrag_core.contracts import HarborConflictError
from harborrag_core.topology import ResolutionDecision, ResolutionRequest

pytestmark = pytest.mark.unit


def _decision(request: ResolutionRequest, revision: int) -> ResolutionDecision:
    # model_construct: the unhandled-action case is deliberately outside the Literal,
    # and nested validation would reject it before the replay under test runs.
    return ResolutionDecision.model_construct(
        request=request,
        revision=revision,
        created_at=datetime.now(UTC),
        resolution_revision=f"revision-{revision}",
    )


def _merge(decision_id: str, *entity_ids: str) -> ResolutionDecision:
    return _decision(
        ResolutionRequest(
            tenant_id="DEFAULT",
            decision_id=decision_id,
            action="merge",
            entity_ids=entity_ids,
            actor="operator",
            reason="same thing",
        ),
        1,
    )


def _revert(decision_id: str, reverts: str) -> ResolutionDecision:
    return _decision(
        ResolutionRequest(
            tenant_id="DEFAULT",
            decision_id=decision_id,
            action="revert",
            reverts_decision_id=reverts,
            actor="operator",
            reason="wrong",
        ),
        2,
    )


def test_a_reverted_merge_still_replays_without_raising() -> None:
    # revert reaches the same branch an unknown action would; it must stay supported.
    mapping = resolution_mapping([_merge("m1", "e1", "e2"), _revert("r1", "m1")])

    # An empty mapping is the identity: resolve_entities reads it as mapping.get(v, v).
    assert mapping == {}


def test_an_unhandled_action_raises_instead_of_replaying_as_identity() -> None:
    # Simulates a future action (e.g. "split") added to the Literal but not to the replay:
    # silently returning an unchanged mapping would log a decision, bump every policy and
    # rebuild the tenant while changing nothing.
    unhandled = ResolutionRequest.model_construct(
        tenant_id="DEFAULT",
        decision_id="s1",
        action="split",
        entity_ids=("e1",),
        reverts_decision_id=None,
        actor="operator",
        reason="conflated",
    )

    with pytest.raises(HarborConflictError, match="split"):
        resolution_mapping([_merge("m1", "e1", "e2"), _decision(unhandled, 2)])
