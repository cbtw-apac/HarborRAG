"""The access service's other views of access: derived data, and the classes of a principal.

Both are held to the same facts and rules as ``authenticate`` and ``check_batch``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.security.classes import QUARANTINE_CLASS, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import Decision
from harborrag_core.security.directory import ProjectMember
from harborrag_engine.security.access_service import ClassAccessService, CorpusPolicy
from harborrag_engine.security.derived import may_serve

from .access_service_helpers import (
    C1,
    C2,
    C3,
    OTHER_REALM,
    OTHER_SCOPE,
    OTHER_TENANT,
    PRINCIPAL,
    REALM,
    SCOPE,
    SHARED,
    TENANT,
    Clock,
    Distrusted,
    Overreaching,
    acl_class,
    build,
    directory,
    doc,
    snapshot,
    token,
    two_datasources,
    world,
)

SHARED_POLICY = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
BOTH = frozenset({SCOPE, OTHER_SCOPE})


# --- check_derived ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_handmade_shared_context_cannot_widen_its_entitlements_over_derived_data():
    service, facts, _ = build(corpus=SHARED_POLICY)
    # RBAC is on and PRINCIPAL is entitled to OTHER_SCOPE only; doc-cpm comes from SCOPE.
    await two_datasources(facts, entitled=frozenset({OTHER_SCOPE}), tenant=SHARED)
    handmade = AccessContext(principal_id=PRINCIPAL, tenant_id=SHARED, corpus_mode="tenant_shared")
    inputs = frozenset({C1})

    # The source document is refused, and the bare rule would still serve what was derived
    # from it, because the context claims no entitlements at all.
    assert await service.check_batch(handmade, [doc("doc-cpm")]) == [
        Decision(False, "not_entitled", C1)
    ]
    cpm = frozenset({SCOPE})
    assert may_serve(handmade, tenant_id=SHARED, classes=inputs, source_scopes=cpm) is True

    served = await service.check_derived(
        handmade, tenant_id=SHARED, classes=inputs, source_scopes=cpm
    )
    assert served is False
    ops = frozenset({OTHER_SCOPE})
    served = await service.check_derived(
        handmade, tenant_id=SHARED, classes=inputs, source_scopes=ops
    )
    assert served is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "context", "scopes", "served"),
    [
        (frozenset({OTHER_SCOPE}), None, frozenset({SCOPE}), False),
        (frozenset({OTHER_SCOPE}), BOTH, BOTH, False),
        (frozenset(), None, frozenset({OTHER_SCOPE}), False),
        (BOTH, frozenset({OTHER_SCOPE}), frozenset({SCOPE}), False),
        (BOTH, None, BOTH, True),
        (None, frozenset({SCOPE}), frozenset({OTHER_SCOPE}), False),
        (None, None, BOTH, True),
    ],
    ids=[
        "context-none-store-narrow",
        "context-wider-than-store",
        "store-empty",
        "context-narrower-than-store",
        "store-wide-context-none",
        "no-rbac-context-narrows",
        "no-rbac-no-gate",
    ],
)
async def test_derived_data_is_served_within_the_stricter_of_context_and_store_entitlements(
    store, context, scopes, served
):
    service, facts, _ = build()
    await two_datasources(facts, entitled=store)
    handmade = AccessContext(
        principal_id=PRINCIPAL,
        tenant_id=TENANT,
        class_ids=frozenset({C1}),
        entitled_source_scopes=context,
    )

    assert (
        await service.check_derived(
            handmade, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=scopes
        )
        is served
    )


@pytest.mark.asyncio
async def test_derived_data_is_never_decided_for_a_context_of_another_corpus_mode():
    source_acl, _, _ = build()
    shared, _, _ = build(corpus=SHARED_POLICY)
    mismatched = [
        (
            source_acl,
            AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT, corpus_mode="tenant_shared"),
        ),
        (shared, AccessContext(principal_id=PRINCIPAL, tenant_id=SHARED)),
        (
            shared,
            AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT, corpus_mode="tenant_shared"),
        ),
        (shared, AccessContext.system(SHARED)),
    ]

    for service, context in mismatched:
        with pytest.raises(HarborAuthError) as refused:
            await service.check_derived(
                context,
                tenant_id=str(context.tenant_id),
                classes=frozenset({C1}),
                source_scopes=frozenset({SCOPE}),
            )
        assert refused.value.forbidden is True


@pytest.mark.asyncio
async def test_honest_contexts_are_served_derived_data_exactly_as_may_serve_says():
    service, facts, _ = build(corpus=SHARED_POLICY)
    for tenant in (TENANT, SHARED):
        await world(
            facts,
            classes=[acl_class(C1, tenant=tenant), acl_class(C2, tenant=tenant)],
            snapshots=[
                snapshot(C1, tenant=tenant),
                snapshot(C2, tenant=tenant, allowed=("hr-bob",)),
            ],
            tenant=tenant,
        )
        await facts.set_project_scopes(tenant, "p1", BOTH)
        await facts.replace_project_members(
            tenant, "p1", [ProjectMember("p1", PRINCIPAL, "reader", frozenset({SCOPE}))]
        )
    contexts = [
        await service.authenticate(token(TENANT, SHARED), tenant_id=TENANT),
        await service.authenticate(token(TENANT, SHARED), tenant_id=SHARED),
        AccessContext.system(TENANT),
    ]
    cases = [
        (TENANT, frozenset({C1}), frozenset({SCOPE})),
        (TENANT, frozenset({C1}), frozenset({OTHER_SCOPE})),
        (TENANT, frozenset({C1, C2}), frozenset({SCOPE})),
        (TENANT, frozenset(), frozenset({SCOPE})),
        (TENANT, frozenset({QUARANTINE_CLASS}), frozenset({SCOPE})),
        (TENANT, frozenset({C1}), frozenset()),
        (SHARED, frozenset({C1, C2}), frozenset({SCOPE})),
        (SHARED, frozenset({C1}), BOTH),
        (OTHER_TENANT, frozenset({C1}), frozenset({SCOPE})),
    ]

    answers = []
    for context in contexts:
        for tenant, classes, scopes in cases:
            expected = may_serve(context, tenant_id=tenant, classes=classes, source_scopes=scopes)
            actual = await service.check_derived(
                context, tenant_id=tenant, classes=classes, source_scopes=scopes
            )
            assert actual is expected, (context.tenant_id, context.purpose, tenant, classes, scopes)
            answers.append(actual)
    # Both answers occur, so the agreement is not vacuous.
    assert True in answers
    assert False in answers


# --- classes_for --------------------------------------------------------------------------------


Fixture = Callable[[], Awaitable[tuple[ClassAccessService, frozenset[ClassId]]]]


async def stale_realm() -> tuple[ClassAccessService, frozenset[ClassId]]:
    """C1 is restricted in a stale realm, C2 restricted in a fresh one, C3 public and stale."""

    service, facts, _ = build()
    await world(
        facts,
        classes=[
            acl_class(C1, realm=REALM),
            acl_class(C2, realm=OTHER_REALM),
            acl_class(C3, realm=REALM),
        ],
        snapshots=[snapshot(C1), snapshot(C2), snapshot(C3, public=True)],
        synced_realms=(REALM, OTHER_REALM),
    )
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=25)))
    return service, frozenset({C2, C3})


async def never_synced() -> tuple[ClassAccessService, frozenset[ClassId]]:
    """No realm was ever synced: only the public class C2 is kept."""

    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1, realm=REALM), acl_class(C2, realm=OTHER_REALM)],
        snapshots=[snapshot(C1), snapshot(C2, public=True)],
        synced_realms=(),
    )
    return service, frozenset({C2})


async def quarantine() -> tuple[ClassAccessService, frozenset[ClassId]]:
    """The quarantine class lets the principal in and is still never among its classes."""

    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2), acl_class(C3), acl_class(QUARANTINE_CLASS)],
        snapshots=[
            snapshot(C1, public=True),
            snapshot(C2),
            snapshot(C3, allowed=("hr-bob",)),
            snapshot(QUARANTINE_CLASS),
        ],
    )
    return service, frozenset({C1, C2})


async def misfiled() -> tuple[ClassAccessService, frozenset[ClassId]]:
    """Only C1's records vouch for it; every other readable class is missing or misfiled."""

    clock = Clock()
    facts = Distrusted(clock=clock)
    service, _, _ = build(facts=facts, clock=clock)
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2)],
        snapshots=[snapshot(C1), snapshot(C3)],
    )
    return service, frozenset({C1})


async def unreadable() -> tuple[ClassAccessService, frozenset[ClassId]]:
    """The store calls C1 readable, but its snapshot denies the principal."""

    clock = Clock()
    facts = Overreaching(clock=clock)
    service, _, _ = build(facts=facts, clock=clock)
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2)],
        snapshots=[snapshot(C1, denied=(PRINCIPAL,)), snapshot(C2)],
    )
    return service, frozenset({C2})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [stale_realm, never_synced, quarantine, misfiled, unreadable],
    ids=["stale-realm", "never-synced", "quarantine", "misfiled", "unreadable"],
)
async def test_classes_for_are_the_class_ids_authenticate_gives(fixture: Fixture):
    service, expected = await fixture()

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == expected
    assert await service.classes_for(PRINCIPAL, TENANT) == expected


@pytest.mark.asyncio
async def test_classes_for_are_empty_in_a_shared_corpus():
    service, facts, _ = build(corpus=SHARED_POLICY)
    await world(
        facts,
        classes=[acl_class(C1, tenant=SHARED)],
        snapshots=[snapshot(C1, tenant=SHARED, public=True)],
        tenant=SHARED,
    )

    context = await service.authenticate(token(SHARED), tenant_id=SHARED)

    assert context.class_ids == frozenset()
    assert await service.classes_for(PRINCIPAL, SHARED) == frozenset()
