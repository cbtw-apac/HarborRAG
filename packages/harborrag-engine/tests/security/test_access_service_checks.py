from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.security.classes import QUARANTINE_CLASS
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import AccessScope, Decision, DocumentAccess
from harborrag_core.security.directory import DecisionLogEntry, ProjectMember
from harborrag_core.security.resources import ResourceRef
from harborrag_core.testing.access_fakes import InMemoryAccessFacts
from harborrag_engine.security.access_service import (
    ClassAccessService,
    CorpusPolicy,
)

from .access_service_helpers import (
    ACTOR,
    C1,
    C2,
    NOW,
    OTHER_SCOPE,
    OTHER_TENANT,
    PRINCIPAL,
    REALM,
    SCOPE,
    SHARED,
    TENANT,
    Clock,
    acl_class,
    build,
    directory,
    doc,
    snapshot,
    token,
    two_datasources,
    world,
)

# --- check_batch ------------------------------------------------------------------------------


async def serving(
    *, synced_ago: timedelta = timedelta(hours=1), actor_id: str | None = None
) -> tuple[ClassAccessService, InMemoryAccessFacts, Clock, AccessContext]:
    """A service where ``doc-1`` is in ``C1``, readable by ``PRINCIPAL``, and ``doc-q`` is quarantined."""

    service, facts, clock = build()
    await world(
        facts,
        classes=[acl_class(C1)],
        snapshots=[snapshot(C1)],
        documents={
            "doc-1": DocumentAccess(C1, SCOPE),
            "doc-q": DocumentAccess(QUARANTINE_CLASS, SCOPE),
        },
    )
    await facts.replace_directory(directory(REALM, synced_ago=synced_ago))
    context = await service.authenticate(token(TENANT, actor_id=actor_id), tenant_id=TENANT)
    return service, facts, clock, context


@pytest.mark.asyncio
async def test_check_batch_records_every_decision():
    service, facts, _, context = await serving(actor_id=ACTOR)
    refs = [
        doc("doc-1"),
        ResourceRef(kind="chunk", id="chunk-9", document_id="doc-1"),
        doc("doc-unknown"),
        ResourceRef(kind="chunk", id="chunk-orphan"),
        doc("doc-q"),
        doc("doc-1"),
    ]

    decisions = await service.check_batch(context, refs)

    assert decisions == [
        Decision(True, "member", C1),
        Decision(True, "member", C1),
        Decision(False, "no_class"),
        Decision(False, "no_class"),
        Decision(False, "quarantined", QUARANTINE_CLASS),
        Decision(True, "member", C1),
    ]
    assert facts.decisions == [
        DecisionLogEntry(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            actor_id=ACTOR,
            resource=ref,
            decision=decision,
            at=NOW,
        )
        for ref, decision in zip(refs, decisions, strict=True)
    ]


@pytest.mark.asyncio
async def test_check_batch_of_nothing_decides_and_records_nothing():
    service, facts, _, context = await serving()

    assert await service.check_batch(context, []) == []
    assert facts.decisions == []


@pytest.mark.asyncio
async def test_check_batch_stamps_every_entry_with_the_one_instant_it_decided_at():
    service, facts, clock, context = await serving()
    clock.now = NOW + timedelta(minutes=10)

    await service.check_batch(context, [doc("doc-1"), doc("doc-unknown")])

    assert [entry.at for entry in facts.decisions] == [clock.now, clock.now]


@pytest.mark.asyncio
async def test_check_batch_does_not_answer_when_the_decisions_cannot_be_logged():
    class Unlogged(InMemoryAccessFacts):
        async def record(self, entries: Sequence[DecisionLogEntry]) -> None:
            raise RuntimeError("the decision log is down")

    clock = Clock()
    facts = Unlogged(clock=clock)
    service, _, _ = build(facts=facts, clock=clock)
    await world(
        facts,
        classes=[acl_class(C1)],
        snapshots=[snapshot(C1)],
        documents={"doc-1": DocumentAccess(C1, SCOPE)},
    )
    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    with pytest.raises(RuntimeError, match="decision log is down"):
        await service.check_batch(context, [doc("doc-1")])


@pytest.mark.asyncio
async def test_check_batch_reads_only_the_contexts_tenant():
    service, facts, _, context = await serving()
    await world(
        facts,
        classes=[acl_class(C1, tenant=OTHER_TENANT)],
        snapshots=[snapshot(C1, tenant=OTHER_TENANT, allowed=())],
        documents={"doc-other": DocumentAccess(C1, SCOPE)},
        tenant=OTHER_TENANT,
    )

    decisions = await service.check_batch(context, [doc("doc-other")])

    # The other tenant's document is not visible from this tenant at all.
    assert decisions == [Decision(False, "no_class")]
    assert [entry.tenant_id for entry in facts.decisions] == [TENANT]


@pytest.mark.asyncio
async def test_check_batch_denies_a_class_that_has_no_snapshot():
    service, facts, _, context = await serving()
    await world(
        facts,
        classes=[acl_class(C2)],
        documents={"doc-2": DocumentAccess(C2, SCOPE)},
    )

    assert await service.check_batch(context, [doc("doc-2")]) == [
        Decision(False, "unknown_snapshot", C2)
    ]


@pytest.mark.asyncio
async def test_a_stale_realms_non_public_class_is_denied_by_check_batch():
    stale_service, _, _, stale_context = await serving(synced_ago=timedelta(hours=25))
    fresh_service, _, _, fresh_context = await serving(synced_ago=timedelta(hours=1))

    assert stale_context.scope.stale_realms == (REALM,)
    assert await stale_service.check_batch(stale_context, [doc("doc-1")]) == [
        Decision(False, "stale_realm", C1)
    ]
    # The identical setup with a fresh directory is readable: staleness is what denies.
    assert await fresh_service.check_batch(fresh_context, [doc("doc-1")]) == [
        Decision(True, "member", C1)
    ]


@pytest.mark.asyncio
async def test_a_public_class_is_still_read_when_its_realm_is_stale():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1)],
        snapshots=[snapshot(C1, public=True)],
        documents={"doc-1": DocumentAccess(C1, SCOPE)},
    )
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(days=3)))
    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.scope.stale_realms == (REALM,)
    assert await service.check_batch(context, [doc("doc-1")]) == [Decision(True, "public", C1)]


@pytest.mark.asyncio
async def test_staleness_is_judged_when_the_batch_is_checked_not_when_the_context_was_built():
    service, facts, clock, context = await serving(synced_ago=timedelta(hours=1))
    assert context.scope.status == "full"
    assert await service.check_batch(context, [doc("doc-1")]) == [Decision(True, "member", C1)]

    clock.now = NOW + timedelta(hours=30)

    assert await service.check_batch(context, [doc("doc-1")]) == [
        Decision(False, "stale_realm", C1)
    ]


@pytest.mark.asyncio
async def test_a_context_that_was_not_built_by_the_service_is_still_held_to_staleness():
    service, facts, _, _ = await serving(synced_ago=timedelta(hours=30))
    handmade = AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT)

    assert handmade.scope.status == "full"
    assert await service.check_batch(handmade, [doc("doc-1")]) == [
        Decision(False, "stale_realm", C1)
    ]


@pytest.mark.asyncio
async def test_a_realm_the_context_names_as_stale_stays_stale_though_its_directory_is_fresh():
    service, _, _, _ = await serving(synced_ago=timedelta(minutes=1))
    claims_stale = AccessContext(
        principal_id=PRINCIPAL,
        tenant_id=TENANT,
        scope=AccessScope.stale([REALM]),
    )

    assert await service.check_batch(claims_stale, [doc("doc-1")]) == [
        Decision(False, "stale_realm", C1)
    ]


@pytest.mark.asyncio
async def test_a_clock_without_a_timezone_is_refused():
    naive = datetime(2026, 10, 17, 12, 0)
    service, _, clock = build()
    context = await service.authenticate(token(TENANT), tenant_id=TENANT)
    clock.now = naive

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.authenticate(token(TENANT), tenant_id=TENANT)
    with pytest.raises(ValueError, match="timezone-aware"):
        await service.check_batch(context, [doc("doc-1")])


@pytest.mark.asyncio
async def test_check_batch_applies_the_contexts_entitlements_and_corpus_mode():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    await world(
        facts,
        classes=[acl_class(C1, tenant=SHARED)],
        snapshots=[snapshot(C1, tenant=SHARED, allowed=())],
        documents={
            "entitled": DocumentAccess(C1, SCOPE),
            "other": DocumentAccess(C1, OTHER_SCOPE),
        },
        tenant=SHARED,
    )
    await facts.set_project_scopes(SHARED, "p1", frozenset({SCOPE, OTHER_SCOPE}))
    await facts.replace_project_members(
        SHARED, "p1", [ProjectMember("p1", PRINCIPAL, "reader", frozenset({SCOPE}))]
    )
    context = await service.authenticate(token(SHARED), tenant_id=SHARED)

    decisions = await service.check_batch(context, [doc("entitled"), doc("other")])

    assert decisions == [
        Decision(True, "tenant_shared", C1),
        Decision(False, "not_entitled", C1),
    ]


# --- a context is held to the service's own policy and facts ----------------------------------


MEMBER = Decision(True, "member", C1)
NOT_ENTITLED = Decision(False, "not_entitled", C1)
BOTH = frozenset({SCOPE, OTHER_SCOPE})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "context", "cpm", "ops"),
    [
        # A context that knows nothing of entitlements cannot widen what the store says.
        (frozenset({OTHER_SCOPE}), None, NOT_ENTITLED, MEMBER),
        (frozenset({OTHER_SCOPE}), BOTH, NOT_ENTITLED, MEMBER),
        (frozenset(), None, NOT_ENTITLED, NOT_ENTITLED),
        (frozenset(), BOTH, NOT_ENTITLED, NOT_ENTITLED),
        # A context may narrow them.
        (BOTH, frozenset({OTHER_SCOPE}), NOT_ENTITLED, MEMBER),
        (BOTH, frozenset(), NOT_ENTITLED, NOT_ENTITLED),
        (BOTH, None, MEMBER, MEMBER),
        # A tenant without RBAC gates nothing, but the context's own narrowing is honoured.
        (None, frozenset({SCOPE}), MEMBER, NOT_ENTITLED),
        (None, None, MEMBER, MEMBER),
    ],
    ids=[
        "context-none-store-narrow",
        "context-wider-than-store",
        "store-empty-context-none",
        "store-empty-context-wide",
        "context-narrower-than-store",
        "context-empty",
        "store-wide-context-none",
        "no-rbac-context-narrows",
        "no-rbac-no-gate",
    ],
)
async def test_a_context_can_narrow_but_never_widen_the_stores_entitlements(
    store, context, cpm, ops
):
    service, facts, _ = build()
    await two_datasources(facts, entitled=store)
    handmade = AccessContext(
        principal_id=PRINCIPAL, tenant_id=TENANT, entitled_source_scopes=context
    )

    decisions = await service.check_batch(handmade, [doc("doc-cpm"), doc("doc-ops")])

    assert decisions == [cpm, ops]


@pytest.mark.asyncio
async def test_a_handmade_context_in_the_shared_corpus_is_held_to_the_stores_entitlements():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    await two_datasources(facts, entitled=frozenset({OTHER_SCOPE}), tenant=SHARED)
    handmade = AccessContext(principal_id=PRINCIPAL, tenant_id=SHARED, corpus_mode="tenant_shared")

    decisions = await service.check_batch(handmade, [doc("doc-cpm"), doc("doc-ops")])

    assert decisions == [
        Decision(False, "not_entitled", C1),
        Decision(True, "tenant_shared", C1),
    ]


@pytest.mark.asyncio
async def test_a_background_context_is_not_gated_by_entitlements():
    service, facts, _ = build()
    await two_datasources(facts, entitled=frozenset())
    background = AccessContext.system(TENANT)

    decisions = await service.check_batch(background, [doc("doc-cpm"), doc("doc-ops")])

    assert decisions == [
        Decision(True, "processing_allowed", C1),
        Decision(True, "processing_allowed", C1),
    ]


@pytest.mark.asyncio
async def test_a_context_of_another_corpus_mode_than_the_policys_is_never_decided():
    service, facts, _ = build()  # source_acl for every tenant
    await world(
        facts,
        classes=[acl_class(C1)],
        snapshots=[snapshot(C1, allowed=("hr-bob",))],
        documents={"doc-1": DocumentAccess(C1, SCOPE)},
    )
    claims_shared = AccessContext(
        principal_id=PRINCIPAL, tenant_id=TENANT, corpus_mode="tenant_shared"
    )

    with pytest.raises(HarborAuthError) as refused:
        await service.check_batch(claims_shared, [doc("doc-1")])

    assert refused.value.forbidden is True
    assert facts.decisions == []
    # The same context with the mode the policy gives the tenant is decided, and denied.
    honest = AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT)
    assert await service.check_batch(honest, [doc("doc-1")]) == [Decision(False, "not_member", C1)]


@pytest.mark.asyncio
async def test_a_context_that_leaves_out_the_shared_mode_is_never_decided():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    await two_datasources(facts, entitled=None, tenant=SHARED)
    shared_tenant_as_source_acl = AccessContext(principal_id=PRINCIPAL, tenant_id=SHARED)
    other_tenant_as_shared = AccessContext(
        principal_id=PRINCIPAL, tenant_id=TENANT, corpus_mode="tenant_shared"
    )

    for mismatched in (shared_tenant_as_source_acl, other_tenant_as_shared):
        with pytest.raises(HarborAuthError) as refused:
            await service.check_batch(mismatched, [doc("doc-cpm")])
        assert refused.value.forbidden is True
    assert facts.decisions == []
    # A background context is held to the same rule: its mode must be the tenant's too.
    with pytest.raises(HarborAuthError):
        await service.check_batch(AccessContext.system(SHARED), [doc("doc-cpm")])
