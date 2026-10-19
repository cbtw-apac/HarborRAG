"""Builders shared by the ClassAccessService tests: one tenant, its realms, classes and
snapshots, and stores whose answers the service must not trust."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from harborrag_core.ports.identity import VerifiedToken
from harborrag_core.security.classes import AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import DocumentAccess
from harborrag_core.security.directory import DirectorySnapshot, ProjectMember
from harborrag_core.security.resources import ResourceRef
from harborrag_core.sources.acl import SourceAcl
from harborrag_core.testing.access_fakes import InMemoryAccessFacts
from harborrag_core.topology.permissions import PermissionResourceKind, ResolvedPermissionSnapshot
from harborrag_engine.security.access_service import (
    AccessLimits,
    ClassAccessService,
    CorpusPolicy,
)

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


def snapshot(  # noqa: PLR0913 - one keyword per snapshot field a case pins
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


async def world(  # noqa: PLR0913 - one keyword per kind of fact a case seeds
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


class Overreaching(InMemoryAccessFacts):
    """A store that calls C1 and C2 readable whatever their snapshots say."""

    async def readable_class_ids(self, access: AccessContext, *, limit: int) -> frozenset[ClassId]:
        return frozenset({C1, C2})


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
