"""Scope-isolation tests for the memory port's ``visible_to`` predicate.

These are the security-relevant contract for ``harborrag-memory``: every
adapter's ``search`` must agree with this pure function, so it is exercised
directly, independent of any storage backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.ports.memory import (
    Memory,
    MemoryMatch,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    ResolvedEntity,
    scope_owner_fields,
    visible_to,
)

_TENANT_A_RUN = MemoryOwner(
    tenant_id="tenant-a",
    project_id="proj-1",
    user_id="user-1",
    principal_id="principal-1",
    session_id="session-1",
    run_id="run-1",
)


@pytest.mark.whitebox
def test_run_scope_requires_every_field_through_run_id() -> None:
    assert scope_owner_fields(MemoryScope.RUN) == (
        "tenant_id",
        "user_id",
        "session_id",
        "run_id",
    )


@pytest.mark.whitebox
def test_user_and_session_scopes_key_on_user_id_not_principal() -> None:
    assert scope_owner_fields(MemoryScope.USER) == ("tenant_id", "user_id")
    assert scope_owner_fields(MemoryScope.SESSION) == ("tenant_id", "user_id", "session_id")
    same_user_other_principal = MemoryOwner(
        tenant_id="tenant-a", user_id="user-1", principal_id="principal-2"
    )
    memory_owner = MemoryOwner(tenant_id="tenant-a", user_id="user-1", principal_id="principal-1")
    assert visible_to(MemoryScope.USER, memory_owner, same_user_other_principal)


@pytest.mark.whitebox
def test_owner_rejects_blank_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        MemoryOwner(tenant_id="tenant-a", user_id=" ")


@pytest.mark.whitebox
def test_run_scoped_memory_is_not_visible_to_a_different_run() -> None:
    caller = MemoryOwner(
        tenant_id="tenant-a",
        project_id="proj-1",
        user_id="user-1",
        principal_id="principal-1",
        session_id="session-1",
        run_id="run-2",
    )
    assert not visible_to(MemoryScope.RUN, _TENANT_A_RUN, caller)


@pytest.mark.whitebox
def test_run_scoped_memory_is_visible_to_its_own_run() -> None:
    assert visible_to(MemoryScope.RUN, _TENANT_A_RUN, _TENANT_A_RUN)


@pytest.mark.whitebox
def test_user_scoped_memory_does_not_cross_tenants() -> None:
    same_user_other_tenant = MemoryOwner(tenant_id="tenant-b", user_id="user-1")
    memory_owner = MemoryOwner(tenant_id="tenant-a", user_id="user-1")
    assert not visible_to(MemoryScope.USER, memory_owner, same_user_other_tenant)


@pytest.mark.whitebox
def test_user_scoped_memory_does_not_cross_users_in_the_same_tenant() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a", user_id="user-1")
    other_user = MemoryOwner(tenant_id="tenant-a", user_id="user-2")
    assert not visible_to(MemoryScope.USER, memory_owner, other_user)


@pytest.mark.whitebox
def test_tenant_scoped_memory_is_visible_to_any_principal_in_the_tenant() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a")
    other_principal = MemoryOwner(tenant_id="tenant-a", principal_id="user-9", run_id="run-9")
    assert visible_to(MemoryScope.TENANT, memory_owner, other_principal)


@pytest.mark.whitebox
def test_global_scoped_memory_is_visible_across_tenants() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a")
    caller = MemoryOwner(tenant_id="tenant-b")
    assert visible_to(MemoryScope.GLOBAL, memory_owner, caller)


@pytest.mark.whitebox
def test_caller_missing_a_required_field_never_matches() -> None:
    memory_owner = MemoryOwner(tenant_id="tenant-a", user_id="user-1", session_id="session-1")
    caller_without_session = MemoryOwner(tenant_id="tenant-a", user_id="user-1")
    assert not visible_to(MemoryScope.SESSION, memory_owner, caller_without_session)


def _memory(**overrides: object) -> Memory:
    values: dict[str, object] = {
        "memory_id": "mem-1",
        "scope": MemoryScope.USER,
        "memory_type": MemoryType.FACT,
        "owner": MemoryOwner(tenant_id="tenant-a", user_id="user-1"),
        "content": "lives in Lyon",
    }
    values.update(overrides)
    return Memory(**values)  # type: ignore[arg-type]


@pytest.mark.whitebox
def test_memory_validity_window_is_half_open() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    memory = _memory(valid_from=start, invalid_at=end, superseded_by="mem-2")

    assert not memory.is_valid_at(start - timedelta(seconds=1))
    assert memory.is_valid_at(start)
    assert memory.is_valid_at(end - timedelta(seconds=1))
    assert not memory.is_valid_at(end)
    assert _memory().is_valid_at(start)
    assert _memory(valid_from=start).is_valid_at(end)


@pytest.mark.whitebox
def test_memory_rejects_inverted_validity_window_and_naive_instants() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="invalid_at must follow valid_from"):
        _memory(valid_from=start, invalid_at=start)
    with pytest.raises(ValueError, match="valid_from must be timezone-aware"):
        _memory(valid_from=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="superseded_by must be non-empty"):
        _memory(superseded_by="")


@pytest.mark.whitebox
def test_memory_provenance_fields_round_trip_as_tuples() -> None:
    memory = _memory(
        source_session_id="session-1",
        source_message_ids=("msg-1", "msg-2"),
        entity_ids=("ent-1",),
        content_hash="abc",
    )
    assert memory.source_message_ids == ("msg-1", "msg-2")
    assert memory.entity_ids == ("ent-1",)
    assert memory.content_hash == "abc"


@pytest.mark.whitebox
def test_memory_query_as_of_must_be_timezone_aware() -> None:
    owner = MemoryOwner(tenant_id="tenant-a", user_id="user-1")
    assert MemoryQuery(owner=owner).include_invalid is False
    assert MemoryQuery(owner=owner, as_of=datetime(2026, 1, 1, tzinfo=UTC)).as_of is not None
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        MemoryQuery(owner=owner, as_of=datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.whitebox
def test_memory_match_rejects_non_finite_scores_and_blank_ids() -> None:
    match = MemoryMatch(memory_id="mem-1", score=-0.25)
    assert (match.memory_id, match.score) == ("mem-1", -0.25)
    for score in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="score must be finite"):
            MemoryMatch(memory_id="mem-1", score=score)
    with pytest.raises(ValueError, match="id must be non-empty"):
        MemoryMatch(memory_id="  ", score=1.0)


@pytest.mark.whitebox
def test_memory_match_is_frozen_and_sorts_by_score() -> None:
    match = MemoryMatch(memory_id="mem-1", score=0.5)
    with pytest.raises(AttributeError):
        match.score = 0.9  # type: ignore[misc]
    matches = (
        MemoryMatch(memory_id="mem-low", score=0.1),
        MemoryMatch(memory_id="mem-high", score=0.9),
    )
    ordered = sorted(matches, key=lambda item: item.score, reverse=True)
    assert [item.memory_id for item in ordered] == ["mem-high", "mem-low"]


@pytest.mark.whitebox
def test_resolved_entity_validates_confidence_and_identity() -> None:
    resolved = ResolvedEntity(mention="Ingest pipeline", entity_id="node-7", confidence=0.75)
    assert (resolved.mention, resolved.entity_id, resolved.confidence) == (
        "Ingest pipeline",
        "node-7",
        0.75,
    )
    for confidence in (float("nan"), float("inf"), float("-inf"), -0.01, 1.01):
        with pytest.raises(ValueError, match="confidence must be finite"):
            ResolvedEntity(mention="m", entity_id="node-7", confidence=confidence)


@pytest.mark.whitebox
def test_resolved_entity_rejects_blank_mention_or_entity_id() -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        ResolvedEntity(mention="  ", entity_id="node-7", confidence=1.0)
    with pytest.raises(ValueError, match="must be non-empty"):
        ResolvedEntity(mention="Ingest", entity_id="  ", confidence=1.0)


@pytest.mark.whitebox
def test_resolved_entity_is_frozen_and_accepts_the_bounds() -> None:
    resolved = ResolvedEntity(mention="Ingest", entity_id="node-7", confidence=0.0)
    assert ResolvedEntity(mention="Ingest", entity_id="node-7", confidence=1.0).confidence == 1.0
    with pytest.raises(AttributeError):
        resolved.confidence = 0.9  # type: ignore[misc]
