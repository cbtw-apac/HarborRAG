from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from harborrag_core.contracts.errors import HarborAccessLimitError, HarborAuthError
from harborrag_core.ports.access import AccessService
from harborrag_core.ports.identity import VerifiedToken
from harborrag_core.security.classes import QUARANTINE_CLASS, AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import AccessScope, Decision, DocumentAccess
from harborrag_core.security.directory import DecisionLogEntry, DirectorySnapshot, ProjectMember
from harborrag_core.security.resources import ResourceRef
from harborrag_core.sources.acl import SourceAcl
from harborrag_core.testing.access_fakes import InMemoryAccessFacts
from harborrag_core.topology.permissions import PermissionResourceKind, ResolvedPermissionSnapshot
from harborrag_engine.security.access_service import (
    AccessLimits,
    ClassAccessService,
    CorpusPolicy,
)
from harborrag_engine.security.derived import may_serve

REALM = "atlassian:acme.atlassian.net"
OTHER_REALM = "atlassian:other.atlassian.net"
SCOPE = "jira-project:CPM"
OTHER_SCOPE = "jira-project:OPS"
TENANT = "tenant-1"
OTHER_TENANT = "tenant-2"
SHARED = "shared"
PRINCIPAL = "hr-alice"
ACTOR = "hr-support"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

C1 = ClassId("class-1")
C2 = ClassId("class-2")
C3 = ClassId("class-3")
C4 = ClassId("class-4")
C5 = ClassId("class-5")
C6 = ClassId("class-6")
C7 = ClassId("class-7")
C8 = ClassId("class-8")

SOURCE_ACL = CorpusPolicy(mode="source_acl", shared_tenant_id=None)


def token(*tenants: str, user_id: str = PRINCIPAL, actor_id: str | None = None) -> VerifiedToken:
    # ``subject`` differs from ``user_id`` so code that reads the wrong one fails.
    return VerifiedToken(
        subject="auth0|subject-of-alice",
        user_id=user_id,
        role="editor",
        tenant_ids=frozenset(tenants),
        actor_id=actor_id,
    )


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def build(
    *,
    corpus: CorpusPolicy = SOURCE_ACL,
    limits: AccessLimits | None = None,
    clock: Clock | None = None,
    facts: InMemoryAccessFacts | None = None,
) -> tuple[ClassAccessService, InMemoryAccessFacts, Clock]:
    clock = Clock() if clock is None else clock
    facts = InMemoryAccessFacts(clock=clock) if facts is None else facts
    service = ClassAccessService(
        facts, corpus=corpus, limits=AccessLimits() if limits is None else limits, clock=clock
    )
    return service, facts, clock


def acl_class(
    class_id: ClassId = C1, *, tenant: str = TENANT, realm: str = REALM, scope: str = SCOPE
) -> AclClass:
    return AclClass(
        tenant_id=tenant, class_id=class_id, acl=SourceAcl.inherited(realm=realm, scope=scope)
    )


def snapshot(
    class_id: ClassId = C1,
    *,
    tenant: str = TENANT,
    public: bool = False,
    allowed: Sequence[str] = (PRINCIPAL,),
    denied: Sequence[str] = (),
    kind: PermissionResourceKind = "class",
    known: bool = True,
    resolved_ago: timedelta = timedelta(hours=1),
    expires_in: timedelta = timedelta(hours=1),
) -> ResolvedPermissionSnapshot:
    return ResolvedPermissionSnapshot(
        tenant_id=tenant,
        resource_kind=kind,
        resource_id=class_id,
        revision="rev-1",
        resolved_at=NOW - resolved_ago,
        expires_at=NOW + expires_in,
        known=known,
        processing_allowed=True,
        public=public,
        allowed_principal_ids=() if public else tuple(allowed),
        denied_principal_ids=tuple(denied),
    )


def directory(
    realm: str = REALM, *, synced_ago: timedelta = timedelta(hours=1), tenant: str = TENANT
) -> DirectorySnapshot:
    return DirectorySnapshot(
        tenant_id=tenant,
        realm=realm,
        synced_at=NOW - synced_ago,
        members={},
        scope_grants={},
        active_users=frozenset(),
    )


def doc(ref_id: str = "doc-1") -> ResourceRef:
    return ResourceRef(kind="document", id=ref_id)


async def world(
    facts: InMemoryAccessFacts,
    *,
    classes: Sequence[AclClass] = (),
    snapshots: Sequence[ResolvedPermissionSnapshot] = (),
    documents: dict[str, DocumentAccess] | None = None,
    tenant: str = TENANT,
    synced_realms: Sequence[str] = (REALM,),
) -> None:
    """Fill ``facts``; every realm of ``synced_realms`` has a fresh directory (override to age it)."""

    await facts.register_classes(list(classes))
    await facts.set_class_snapshots(list(snapshots))
    await facts.assign_documents(tenant, documents or {})
    for realm in synced_realms:
        await facts.replace_directory(directory(realm, tenant=tenant))


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


class Distrusted(InMemoryAccessFacts):
    """A store whose readable classes include ones its records cannot vouch for."""

    async def readable_class_ids(self, access: AccessContext, *, limit: int) -> frozenset[ClassId]:
        return frozenset({C1, C2, C3, C4, C5, C6, C7, C8})

    async def classes(
        self, tenant_id: str, class_ids: Sequence[ClassId] | None = None
    ) -> dict[ClassId, AclClass]:
        found = await super().classes(tenant_id, class_ids)
        found[C4] = acl_class(C1)  # filed under another class's key
        found[C5] = acl_class(C5)
        found[C6] = acl_class(C6, tenant=OTHER_TENANT)  # another tenant's class
        found[C7] = acl_class(C7)
        found[C8] = acl_class(C8)
        return found

    async def class_snapshots(
        self, tenant_id: str, class_ids: Sequence[ClassId]
    ) -> dict[ClassId, ResolvedPermissionSnapshot]:
        found = await super().class_snapshots(tenant_id, class_ids)
        found[C4] = snapshot(C4)
        found[C5] = snapshot(C1)  # another class's snapshot
        found[C6] = snapshot(C6)
        found[C7] = snapshot(C7, tenant=OTHER_TENANT)  # another tenant's snapshot
        found[C8] = snapshot(C8, kind="document")  # a snapshot of something else
        return found


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


class Overreaching(InMemoryAccessFacts):
    """A store that calls C1 and C2 readable whatever their snapshots say."""

    async def readable_class_ids(self, access: AccessContext, *, limit: int) -> frozenset[ClassId]:
        return frozenset({C1, C2})


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


async def two_datasources(
    facts: InMemoryAccessFacts, *, entitled: frozenset[str] | None, tenant: str = TENANT
) -> None:
    """``doc-cpm`` (SCOPE) and ``doc-ops`` (OTHER_SCOPE), both in C1, which PRINCIPAL may read.

    ``entitled`` is what the entitlement store says about PRINCIPAL: ``None`` leaves the tenant
    without RBAC, a set puts PRINCIPAL in a project with exactly those datasources (the empty set
    leaves RBAC on with PRINCIPAL in no project).
    """

    await world(
        facts,
        classes=[acl_class(C1, tenant=tenant)],
        snapshots=[snapshot(C1, tenant=tenant)],
        documents={
            "doc-cpm": DocumentAccess(C1, SCOPE),
            "doc-ops": DocumentAccess(C1, OTHER_SCOPE),
        },
        tenant=tenant,
    )
    if entitled is not None:
        member = PRINCIPAL if entitled else "hr-bob"
        # Narrowing is bounded by the project, so the project holds both datasources.
        await facts.set_project_scopes(tenant, "p1", frozenset({SCOPE, OTHER_SCOPE}))
        await facts.replace_project_members(
            tenant, "p1", [ProjectMember("p1", member, "reader", entitled or frozenset({SCOPE}))]
        )


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
