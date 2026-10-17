from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from types import MappingProxyType

import pytest

from harborrag_core.contracts.errors import HarborAccessLimitError, HarborAuthError
from harborrag_core.ports.access import AccessService
from harborrag_core.security.classes import QUARANTINE_CLASS, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import Decision, DocumentAccess
from harborrag_core.security.directory import ProjectMember
from harborrag_core.testing.access_fakes import InMemoryAccessFacts
from harborrag_engine.security.access_service import (
    AccessLimits,
    ClassAccessService,
    CorpusPolicy,
)
from harborrag_engine.security.derived import may_serve

from .access_service_helpers import (
    ACTOR,
    C1,
    C2,
    C3,
    NOW,
    OTHER_REALM,
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
    world,
)

# --- the corpus policy ------------------------------------------------------------------------


def test_the_corpus_policy_is_shared_for_one_tenant_only():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)

    assert policy.mode_for(SHARED) == "tenant_shared"
    assert policy.mode_for(TENANT) == "source_acl"
    assert policy.mode_for("") == "source_acl"
    # A source_acl policy stays source_acl even for the tenant it names.
    assert CorpusPolicy("source_acl", SHARED).mode_for(SHARED) == "source_acl"
    assert CorpusPolicy("source_acl", None).mode_for(SHARED) == "source_acl"
    # Tenant ids are matched exactly: another case is another tenant.
    assert policy.mode_for(SHARED.upper()) == "source_acl"
    assert policy.mode_for(SHARED.title()) == "source_acl"


@pytest.mark.parametrize(
    ("mode", "shared_tenant_id"),
    [
        ("tenant_shared", None),
        ("tenant_shared", ""),
        ("tenant_shared", "  "),
        ("tenant_shared", "*"),
        ("tenant_shared", 7),
        ("source_acl", ""),
        ("source_acl", 7),
        ("shared", SHARED),
        (None, None),
    ],
)
def test_a_corpus_policy_that_cannot_be_enforced_is_refused(mode, shared_tenant_id):
    with pytest.raises(ValueError):
        CorpusPolicy(mode=mode, shared_tenant_id=shared_tenant_id)


@pytest.mark.asyncio
async def test_only_the_shared_tenant_reads_the_shared_corpus():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    # A zero limit makes ``readable_class_ids`` raise for any readable class, so the
    # shared tenant authenticating at all proves the classes were never asked for.
    service, facts, _ = build(corpus=policy, limits=AccessLimits(max_classes_per_user=0))
    await world(
        facts,
        classes=[acl_class(C1, tenant=SHARED)],
        snapshots=[snapshot(C1, tenant=SHARED)],
    )

    shared = await service.authenticate(token(SHARED, TENANT), tenant_id=SHARED)

    assert shared.corpus_mode == "tenant_shared"
    assert shared.tenant_id == SHARED
    assert shared.class_ids == frozenset()

    # Any other tenant is source_acl and has its classes computed.
    relaxed, other_facts, _ = build(corpus=policy)
    await world(
        other_facts,
        classes=[acl_class(C1, tenant=TENANT)],
        snapshots=[snapshot(C1, tenant=TENANT)],
    )
    other = await relaxed.authenticate(token(SHARED, TENANT), tenant_id=TENANT)

    assert other.corpus_mode == "source_acl"
    assert other.class_ids == frozenset({C1})


# --- which tenant a token acts in -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_the_token_does_not_grant_is_refused():
    service, _, _ = build()

    with pytest.raises(HarborAuthError) as refused:
        await service.authenticate(token(TENANT), tenant_id=OTHER_TENANT)

    assert refused.value.forbidden is True


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [TENANT.upper(), TENANT.title()])
async def test_a_tenant_is_granted_exactly_not_by_case(requested):
    service, _, _ = build()

    with pytest.raises(HarborAuthError) as refused:
        await service.authenticate(token(TENANT), tenant_id=requested)

    assert refused.value.forbidden is True


@pytest.mark.asyncio
async def test_a_wildcard_token_is_granted_any_named_tenant():
    service, _, _ = build()

    context = await service.authenticate(token("*"), tenant_id=OTHER_TENANT)

    assert context.tenant_id == OTHER_TENANT


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["*", "", " ", f" {TENANT}", f"{TENANT} ", f"{TENANT}\n"])
async def test_a_tenant_that_is_not_a_plain_tenant_id_is_refused(requested):
    service, _, _ = build()

    with pytest.raises(HarborAuthError) as refused:
        await service.authenticate(token("*", TENANT), tenant_id=requested)

    assert refused.value.forbidden is True


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [(TENANT,), (TENANT, "*")])
async def test_a_token_with_one_tenant_needs_no_tenant_argument(granted):
    service, _, _ = build()

    context = await service.authenticate(token(*granted), tenant_id=None)

    assert context.tenant_id == TENANT


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [("*",), (TENANT, OTHER_TENANT), (TENANT, OTHER_TENANT, "*")])
async def test_a_token_without_exactly_one_named_tenant_needs_a_tenant_argument(granted):
    service, _, _ = build()

    with pytest.raises(HarborAuthError) as refused:
        await service.authenticate(token(*granted), tenant_id=None)

    assert refused.value.forbidden is True


@pytest.mark.asyncio
async def test_the_context_names_the_principal_the_actor_and_the_tenant():
    service, _, _ = build()

    context = await service.authenticate(token(TENANT, actor_id=ACTOR), tenant_id=TENANT)

    assert context.principal_id == PRINCIPAL
    assert context.actor_id == ACTOR
    assert context.tenant_id == TENANT
    assert context.corpus_mode == "source_acl"
    assert context.purpose == "serve"
    assert context.scope.status == "full"

    anonymous_actor = await service.authenticate(token(TENANT), tenant_id=TENANT)
    assert anonymous_actor.actor_id is None


# --- the classes a principal reads ------------------------------------------------------------


@pytest.mark.asyncio
async def test_class_ids_hold_the_readable_classes_public_ones_included_and_never_quarantine():
    service, facts, _ = build()
    await world(
        facts,
        # The quarantine class has a class record and a snapshot that lets alice in, so only
        # the service's own exclusion of it can keep it out of ``class_ids``.
        classes=[acl_class(C1), acl_class(C2), acl_class(C3), acl_class(QUARANTINE_CLASS)],
        snapshots=[
            snapshot(C1, public=True),
            snapshot(C2, allowed=(PRINCIPAL,)),
            snapshot(C3, allowed=("hr-bob",)),
            snapshot(QUARANTINE_CLASS, allowed=(PRINCIPAL,)),
        ],
    )

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C1, C2})
    # The raw store answer does contain it: the service is what removes it.
    raw = await facts.readable_class_ids(
        AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT), limit=10
    )
    assert QUARANTINE_CLASS in raw


@pytest.mark.asyncio
async def test_too_many_classes_fail_closed():
    service, facts, _ = build(limits=AccessLimits(max_classes_per_user=2))
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2), acl_class(C3)],
        snapshots=[snapshot(C1), snapshot(C2), snapshot(C3)],
    )

    with pytest.raises(HarborAccessLimitError):
        await service.authenticate(token(TENANT), tenant_id=TENANT)


@pytest.mark.asyncio
async def test_classes_up_to_the_limit_are_returned_whole():
    service, facts, _ = build(limits=AccessLimits(max_classes_per_user=2))
    await world(
        facts, classes=[acl_class(C1), acl_class(C2)], snapshots=[snapshot(C1), snapshot(C2)]
    )

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C1, C2})


@pytest.mark.asyncio
async def test_a_store_that_ignores_the_limit_still_fails_closed():
    class Unbounded(InMemoryAccessFacts):
        async def readable_class_ids(
            self, access: AccessContext, *, limit: int
        ) -> frozenset[ClassId]:
            return frozenset({C1, C2, C3})

    clock = Clock()
    service, _, _ = build(
        limits=AccessLimits(max_classes_per_user=2), facts=Unbounded(clock=clock), clock=clock
    )

    with pytest.raises(HarborAccessLimitError):
        await service.authenticate(token(TENANT), tenant_id=TENANT)
    with pytest.raises(HarborAccessLimitError):
        await service.classes_for(PRINCIPAL, TENANT)


@pytest.mark.asyncio
async def test_classes_for_names_the_readable_classes_without_quarantine():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2), acl_class(C3), acl_class(QUARANTINE_CLASS)],
        snapshots=[
            snapshot(C1, public=True),
            snapshot(C2),
            snapshot(C3, allowed=("hr-bob",)),
            snapshot(QUARANTINE_CLASS),
            snapshot(C2, tenant=OTHER_TENANT),
        ],
    )

    assert await service.classes_for(PRINCIPAL, TENANT) == frozenset({C1, C2})
    assert await service.classes_for("hr-bob", TENANT) == frozenset({C1, C3})
    assert await service.classes_for(PRINCIPAL, "tenant-3") == frozenset()


@pytest.mark.asyncio
async def test_classes_for_fails_closed_above_the_limit():
    service, facts, _ = build(limits=AccessLimits(max_classes_per_user=1))
    await world(facts, snapshots=[snapshot(C1), snapshot(C2)])

    with pytest.raises(HarborAccessLimitError):
        await service.classes_for(PRINCIPAL, TENANT)


@pytest.mark.asyncio
async def test_classes_for_refuses_a_tenant_that_is_not_a_plain_tenant_id():
    service, _, _ = build()

    for tenant in ("", " ", f" {TENANT}", "*"):
        with pytest.raises(ValueError):
            await service.classes_for(PRINCIPAL, tenant)


# --- entitlements -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entitlements_are_loaded_in_both_corpus_modes():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    for tenant in (TENANT, SHARED):
        await facts.set_project_scopes(tenant, "p1", frozenset({SCOPE}))
        await facts.replace_project_members(
            tenant, "p1", [ProjectMember("p1", PRINCIPAL, "reader", frozenset({SCOPE}))]
        )

    source_acl = await service.authenticate(token(TENANT, SHARED), tenant_id=TENANT)
    shared = await service.authenticate(token(TENANT, SHARED), tenant_id=SHARED)

    assert source_acl.corpus_mode == "source_acl"
    assert source_acl.entitled_source_scopes == frozenset({SCOPE})
    assert shared.corpus_mode == "tenant_shared"
    assert shared.entitled_source_scopes == frozenset({SCOPE})


@pytest.mark.asyncio
async def test_entitlements_are_none_only_for_a_tenant_without_rbac():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    await facts.replace_project_members(
        OTHER_TENANT, "p1", [ProjectMember("p1", "hr-bob", "reader", frozenset({SCOPE}))]
    )

    never_configured = await service.authenticate(token(TENANT, SHARED), tenant_id=TENANT)
    never_configured_shared = await service.authenticate(token(TENANT, SHARED), tenant_id=SHARED)
    configured_no_member = await service.authenticate(token(OTHER_TENANT), tenant_id=OTHER_TENANT)

    assert never_configured.entitled_source_scopes is None
    assert never_configured_shared.entitled_source_scopes is None
    # RBAC is on for the tenant and this principal is in no project: nothing is entitled.
    assert configured_no_member.entitled_source_scopes == frozenset()


# --- stale realms -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_realm_marks_the_scope_stale():
    service, facts, _ = build()
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=25)))
    await facts.replace_directory(directory(OTHER_REALM, synced_ago=timedelta(hours=1)))

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.scope.status == "stale"
    # An exact tuple of realms, not the characters of a string.
    assert context.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_stale_realms_are_listed_sorted():
    service, facts, _ = build()
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(days=2)))
    await facts.replace_directory(directory(OTHER_REALM, synced_ago=timedelta(days=2)))
    await facts.replace_directory(directory("entra:aaaa", synced_ago=timedelta(days=2)))

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.scope.stale_realms == tuple(sorted([REALM, OTHER_REALM, "entra:aaaa"]))


@pytest.mark.asyncio
async def test_a_realm_is_stale_only_once_it_is_older_than_its_slo():
    service, facts, clock = build()
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=24)))

    at_the_slo = await service.authenticate(token(TENANT), tenant_id=TENANT)
    clock.now = NOW + timedelta(microseconds=1)
    past_the_slo = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert at_the_slo.scope.status == "full"
    assert at_the_slo.scope.stale_realms == ()
    assert past_the_slo.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_a_realm_has_its_own_revocation_slo():
    limits = AccessLimits(
        default_revocation_slo=timedelta(hours=24),
        revocation_slo={REALM: timedelta(hours=1)},
    )
    service, facts, _ = build(limits=limits)
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=2)))
    await facts.replace_directory(directory(OTHER_REALM, synced_ago=timedelta(hours=2)))

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_fresh_realms_leave_the_scope_full():
    service, facts, _ = build()
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(minutes=5)))
    await facts.replace_directory(
        directory(OTHER_REALM, tenant=OTHER_TENANT, synced_ago=timedelta(days=9))
    )

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    # Another tenant's stale directory says nothing about this tenant.
    assert context.scope.status == "full"
    assert context.scope.stale_realms == ()


# --- classes of stale realms ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restricted_class_of_a_stale_realm_is_not_in_class_ids():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1, realm=REALM), acl_class(C2, realm=OTHER_REALM)],
        snapshots=[snapshot(C1), snapshot(C2)],
        synced_realms=(REALM, OTHER_REALM),
    )
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=25)))

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C2})
    assert context.scope.status == "stale"
    assert context.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_a_public_class_of_a_stale_realm_stays_in_class_ids():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2)],
        snapshots=[snapshot(C1, public=True), snapshot(C2)],
    )
    await facts.replace_directory(directory(REALM, synced_ago=timedelta(days=3)))

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C1})
    assert context.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_a_realm_with_no_sync_record_is_stale():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1, realm=REALM), acl_class(C2, realm=OTHER_REALM)],
        snapshots=[snapshot(C1), snapshot(C2, public=True)],
        documents={"doc-1": DocumentAccess(C1, SCOPE), "doc-2": DocumentAccess(C2, SCOPE)},
        synced_realms=(),
    )
    assert await facts.realm_sync_times(TENANT) == {}

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    # The restricted class goes; the public one stays, though its realm is just as unknown.
    assert context.class_ids == frozenset({C2})
    assert context.scope.status == "stale"
    assert context.scope.stale_realms == tuple(sorted([REALM, OTHER_REALM]))

    # check_batch reaches the same verdict on its own, from a context that claims nothing.
    handmade = AccessContext(
        principal_id=PRINCIPAL, tenant_id=TENANT, class_ids=frozenset({C1, C2})
    )
    assert await service.check_batch(handmade, [doc("doc-1"), doc("doc-2")]) == [
        Decision(False, "stale_realm", C1),
        Decision(True, "public", C2),
    ]


@pytest.mark.asyncio
async def test_check_batch_judges_the_realms_of_the_classes_it_decides():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1, realm=REALM), acl_class(C2, realm=OTHER_REALM)],
        snapshots=[snapshot(C1), snapshot(C2)],
        documents={"doc-1": DocumentAccess(C1, SCOPE), "doc-2": DocumentAccess(C2, SCOPE)},
        synced_realms=(REALM,),
    )
    handmade = AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT)

    # OTHER_REALM has no sync record, REALM is fresh: only the class of OTHER_REALM is stale.
    assert await service.check_batch(handmade, [doc("doc-1"), doc("doc-2")]) == [
        Decision(True, "member", C1),
        Decision(False, "stale_realm", C2),
    ]


@pytest.mark.asyncio
async def test_a_readable_class_the_records_cannot_vouch_for_is_dropped():
    clock = Clock()
    facts = Distrusted(clock=clock)
    service, _, _ = build(facts=facts, clock=clock)
    # C1 is whole. C2 has a class but no snapshot, C3 a snapshot but no class. C4's class is
    # filed under another class's key, C5's snapshot is another class's, C6 is another tenant's,
    # C7's snapshot is another tenant's and C8's is a document's.
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2)],
        snapshots=[snapshot(C1), snapshot(C3)],
    )

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C1})
    # None of them says anything about a realm, so none can name one as stale.
    assert context.scope.status == "full"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusing",
    [
        {"denied": (PRINCIPAL,)},
        {"allowed": ("hr-bob",)},
        {"resolved_ago": timedelta(hours=2), "expires_in": timedelta(hours=-1)},
        {"resolved_ago": timedelta(hours=-1), "expires_in": timedelta(hours=2)},
        {"known": False},
    ],
    ids=["denied", "not-a-member", "expired", "not-yet-valid", "unknown"],
)
async def test_a_readable_class_whose_snapshot_refuses_the_principal_is_dropped(refusing):
    clock = Clock()
    facts = Overreaching(clock=clock)
    service, _, _ = build(facts=facts, clock=clock)
    await world(
        facts,
        classes=[acl_class(C1), acl_class(C2)],
        snapshots=[snapshot(C1, **refusing), snapshot(C2)],
    )
    # The store's own answer is the problem: it still names C1.
    assert C1 in await facts.readable_class_ids(
        AccessContext(principal_id=PRINCIPAL, tenant_id=TENANT), limit=10
    )

    context = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert context.class_ids == frozenset({C2})


@pytest.mark.asyncio
async def test_a_shared_corpus_scope_still_names_the_realms_past_their_slo():
    policy = CorpusPolicy(mode="tenant_shared", shared_tenant_id=SHARED)
    service, facts, _ = build(corpus=policy)
    await facts.replace_directory(directory(REALM, tenant=SHARED, synced_ago=timedelta(hours=25)))
    await facts.replace_directory(directory(OTHER_REALM, tenant=SHARED))

    context = await service.authenticate(token(SHARED), tenant_id=SHARED)

    assert context.class_ids == frozenset()
    assert context.scope.status == "stale"
    assert context.scope.stale_realms == (REALM,)


@pytest.mark.asyncio
async def test_a_stale_realms_restricted_class_no_longer_serves_derived_data():
    service, facts, _ = build()
    await world(
        facts,
        classes=[acl_class(C1, realm=REALM), acl_class(C2, realm=OTHER_REALM)],
        snapshots=[snapshot(C1), snapshot(C2)],
        synced_realms=(REALM, OTHER_REALM),
    )
    inputs = frozenset({C1, C2})
    scopes = frozenset({SCOPE})

    fresh = await service.authenticate(token(TENANT), tenant_id=TENANT)
    assert may_serve(fresh, tenant_id=TENANT, classes=inputs, source_scopes=scopes) is True

    await facts.replace_directory(directory(REALM, synced_ago=timedelta(hours=25)))
    stale = await service.authenticate(token(TENANT), tenant_id=TENANT)

    assert may_serve(stale, tenant_id=TENANT, classes=inputs, source_scopes=scopes) is False
    assert (
        may_serve(stale, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=scopes) is False
    )
    assert may_serve(stale, tenant_id=TENANT, classes=frozenset({C2}), source_scopes=scopes) is True


def test_limits_are_read_only_and_validated():
    slos = {REALM: timedelta(hours=1)}
    limits = AccessLimits(revocation_slo=slos)
    slos[OTHER_REALM] = timedelta(hours=2)

    assert isinstance(limits.revocation_slo, MappingProxyType)
    assert dict(limits.revocation_slo) == {REALM: timedelta(hours=1)}
    assert AccessLimits().max_classes_per_user == 5000
    assert AccessLimits().default_revocation_slo == timedelta(hours=24)
    for bad in (
        {"max_classes_per_user": -1},
        {"max_classes_per_user": True},
        {"max_classes_per_user": 1.5},
        {"default_revocation_slo": timedelta(0)},
        {"default_revocation_slo": timedelta(hours=-1)},
        {"default_revocation_slo": 3600},
        {"revocation_slo": {REALM: timedelta(0)}},
        {"revocation_slo": {REALM: 60}},
        {"revocation_slo": {"": timedelta(hours=1)}},
        {"revocation_slo": [REALM]},
    ):
        with pytest.raises(ValueError):
            AccessLimits(**bad)


# --- the service is an AccessService, and the package exports ---------------------------------


def _conforms(service: ClassAccessService) -> AccessService:
    """Fail type checking if the service stops satisfying the port."""

    return service


def test_the_public_api_is_exported_from_the_security_package():
    import harborrag_engine.security as security

    expected = {
        "MAX_LIFETIME",
        "MIN_LIFETIME",
        "ApiKeyManagementService",
        "ApiKeyPolicyError",
        "ApiKeyVerificationService",
        "CreatedKey",
        "VerifiedKey",
        "canonical_acl",
        "acl_fingerprint",
        "class_of",
        "account_index",
        "expand",
        "expand_all",
        "ClassReaders",
        "class_snapshot",
        "StageDecision",
        "stage_decision",
        "classes_of",
        "may_serve",
        "AccessLimits",
        "ClassAccessService",
        "CorpusPolicy",
    }

    assert set(security.__all__) == expected
    for name in expected:
        assert getattr(security, name) is not None, name
    assert security.ClassAccessService is ClassAccessService
    assert security.CorpusPolicy is CorpusPolicy
    assert security.AccessLimits is AccessLimits


def test_the_exports_do_not_shadow_the_decide_and_class_readers_modules():
    # A fresh interpreter: in this process the submodules are long since imported.
    probe = (
        "import types, harborrag_engine.security as s;"
        "print(isinstance(s.decide, types.ModuleType), isinstance(s.class_readers, types.ModuleType));"
        "from harborrag_engine.security.decide import decide;"
        "from harborrag_engine.security.class_readers import class_readers;"
        "print(callable(decide), callable(class_readers))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )

    assert result.stdout.split("\n")[:2] == ["True True", "True True"]
