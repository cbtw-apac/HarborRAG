"""Access ports, the directory model and the in-memory access facts fake."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.contracts.errors import (
    HarborAccessLimitError,
    HarborAuthorizationUnavailableError,
)
from harborrag_core.ports.access import (
    AccessClassStore,
    AccessFactsPort,
    DecisionLog,
    DirectoryStore,
    EntitlementStore,
)
from harborrag_core.ports.identity import VerifiedToken
from harborrag_core.security import (
    QUARANTINE_CLASS,
    AccessContext,
    AclClass,
    ClassId,
    Decision,
    DocumentAccess,
    ResourceRef,
)
from harborrag_core.security.directory import (
    ChangeBatch,
    DecisionLogEntry,
    DirectorySnapshot,
    IdentityLink,
    ProjectMember,
)
from harborrag_core.sources import SourceAcl, SourcePrincipal
from harborrag_core.testing.access_fakes import InMemoryAccessFacts
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

REALM = "atlassian:acme.atlassian.net"
OTHER_REALM = "atlassian:other.atlassian.net"
SCOPE = "jira-project:CPM"
NOW = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)

C_PUBLIC = ClassId("c-public")
C_MEMBER = ClassId("c-member")
C_DENIED = ClassId("c-denied")
C_EXPIRED = ClassId("c-expired")
C_UNKNOWN = ClassId("c-unknown")


def _user(id_: str, realm: str = REALM) -> SourcePrincipal:
    return SourcePrincipal(realm=realm, kind="user", id=id_)


def _group(id_: str, realm: str = REALM) -> SourcePrincipal:
    return SourcePrincipal(realm=realm, kind="group", id=id_)


def _facts(now: datetime = NOW) -> InMemoryAccessFacts:
    return InMemoryAccessFacts(clock=lambda: now)


def _access(principal_id: str = "u1", tenant_id: str = "t1") -> AccessContext:
    return AccessContext(principal_id=principal_id, tenant_id=tenant_id)


def _snapshot(
    class_id: ClassId,
    *,
    tenant_id: str = "t1",
    kind: str = "class",
    known: bool = True,
    public: bool = False,
    allowed: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
    resolved_at: datetime = NOW - timedelta(hours=1),
    expires_at: datetime = NOW + timedelta(hours=1),
) -> ResolvedPermissionSnapshot:
    return ResolvedPermissionSnapshot(
        tenant_id=tenant_id,
        resource_kind=kind,  # type: ignore[arg-type]
        resource_id=class_id,
        revision="r1",
        resolved_at=resolved_at,
        expires_at=expires_at,
        known=known,
        public=public,
        allowed_principal_ids=allowed,
        denied_principal_ids=denied,
    )


def _class(class_id: ClassId, *, tenant_id: str = "t1", group: str = "g1") -> AclClass:
    acl = SourceAcl.restricted(
        realm=REALM, scope=SCOPE, levels=[[_group(group)]], reason="restricted"
    )
    return AclClass(tenant_id=tenant_id, class_id=class_id, acl=acl)


def _directory(**overrides: object) -> DirectorySnapshot:
    fields: dict[str, object] = {
        "tenant_id": "t1",
        "realm": REALM,
        "synced_at": NOW,
        "members": {_group("g1"): frozenset({_user("a1"), _group("g2")})},
        "scope_grants": {SCOPE: frozenset({_group("g1")})},
        "active_users": frozenset({_user("a1")}),
    }
    fields.update(overrides)
    return DirectorySnapshot(**fields)  # type: ignore[arg-type]


async def _facts_with_classes() -> InMemoryAccessFacts:
    facts = _facts()
    await facts.set_class_snapshots(
        [
            _snapshot(C_PUBLIC, public=True),
            _snapshot(C_MEMBER, allowed=("u1",)),
            _snapshot(C_DENIED, allowed=("u1",), denied=("u1",)),
            _snapshot(
                C_EXPIRED,
                allowed=("u1",),
                resolved_at=NOW - timedelta(hours=2),
                expires_at=NOW - timedelta(hours=1),
            ),
            _snapshot(C_UNKNOWN, known=False, allowed=("u1",)),
        ]
    )
    return facts


# Document access ---------------------------------------------------------


@pytest.mark.asyncio
async def test_assigned_documents_resolve_to_their_class():
    facts = _facts()
    await facts.assign_documents("t1", {"d1": DocumentAccess(C_MEMBER, "scope-a")})

    assert await facts.document_access("t1", ["d1", "d2"]) == {
        "d1": DocumentAccess(C_MEMBER, "scope-a")
    }


@pytest.mark.asyncio
async def test_document_access_never_crosses_tenants():
    facts = _facts()
    await facts.assign_documents("t1", {"d1": DocumentAccess(C_MEMBER, "scope-a")})

    assert await facts.document_access("t2", ["d1"]) == {}


@pytest.mark.asyncio
async def test_reassigning_a_document_replaces_its_class():
    facts = _facts()
    await facts.assign_documents("t1", {"d1": DocumentAccess(C_MEMBER, "scope-a")})
    await facts.assign_documents("t1", {"d1": DocumentAccess(QUARANTINE_CLASS, "scope-a")})

    assert await facts.document_access("t1", ["d1"]) == {
        "d1": DocumentAccess(QUARANTINE_CLASS, "scope-a")
    }


# Classes and their snapshots ----------------------------------------------


@pytest.mark.asyncio
async def test_registered_classes_are_listed_per_tenant_and_by_id():
    facts = _facts()
    c1, c2, other = _class(C_MEMBER), _class(C_PUBLIC, group="g2"), _class(C_MEMBER, tenant_id="t2")
    await facts.register_classes([c1, c2, other])

    assert await facts.classes("t1") == {C_MEMBER: c1, C_PUBLIC: c2}
    assert await facts.classes("t1", [C_PUBLIC, C_DENIED]) == {C_PUBLIC: c2}
    assert await facts.classes("t1", []) == {}
    assert await facts.classes("t2") == {C_MEMBER: other}
    assert await facts.classes("t3") == {}


@pytest.mark.asyncio
async def test_registering_a_class_again_is_idempotent():
    facts = _facts()
    await facts.register_classes([_class(C_MEMBER)])
    await facts.register_classes([_class(C_MEMBER)])

    assert await facts.classes("t1") == {C_MEMBER: _class(C_MEMBER)}


@pytest.mark.asyncio
async def test_registering_a_different_acl_under_a_known_class_id_fails_closed():
    facts = _facts()
    await facts.register_classes([_class(C_MEMBER, group="g1")])

    with pytest.raises(ValueError, match="already registered"):
        await facts.register_classes([_class(C_MEMBER, group="g2")])

    assert await facts.classes("t1") == {C_MEMBER: _class(C_MEMBER, group="g1")}


@pytest.mark.asyncio
async def test_class_snapshots_are_returned_for_the_requested_classes_only():
    facts = _facts()
    member, public = _snapshot(C_MEMBER, allowed=("u1",)), _snapshot(C_PUBLIC, public=True)
    await facts.set_class_snapshots([member, public, _snapshot(C_DENIED, tenant_id="t2")])

    assert await facts.class_snapshots("t1", [C_MEMBER, C_DENIED]) == {C_MEMBER: member}
    assert await facts.class_snapshots("t1", []) == {}
    assert await facts.class_snapshots("t2", [C_DENIED]) == {
        C_DENIED: _snapshot(C_DENIED, tenant_id="t2")
    }


@pytest.mark.asyncio
async def test_a_newer_class_snapshot_replaces_the_older_one():
    facts = _facts()
    await facts.set_class_snapshots([_snapshot(C_MEMBER, allowed=("u1",))])
    newer = _snapshot(C_MEMBER, allowed=("u2",))
    await facts.set_class_snapshots([newer])

    assert await facts.class_snapshots("t1", [C_MEMBER]) == {C_MEMBER: newer}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "document"])
async def test_only_class_snapshots_are_accepted_by_the_class_store(kind: str):
    facts = _facts()

    with pytest.raises(ValueError, match="class snapshot"):
        await facts.set_class_snapshots([_snapshot(C_PUBLIC, kind=kind, public=True)])

    assert await facts.class_snapshots("t1", [C_PUBLIC]) == {}
    assert await facts.readable_class_ids(_access(), limit=10) == frozenset()


# Readable classes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_readable_classes_follow_snapshot_rules():
    facts = await _facts_with_classes()

    assert await facts.readable_class_ids(_access("u1"), limit=10) == {C_PUBLIC, C_MEMBER}
    assert await facts.readable_class_ids(_access("u2"), limit=10) == {C_PUBLIC}


@pytest.mark.asyncio
async def test_readable_classes_above_the_limit_fail_closed():
    facts = await _facts_with_classes()

    with pytest.raises(HarborAccessLimitError):
        await facts.readable_class_ids(_access("u1"), limit=1)


@pytest.mark.asyncio
async def test_readable_classes_at_the_limit_are_returned_whole():
    facts = await _facts_with_classes()

    assert await facts.readable_class_ids(_access("u1"), limit=2) == {C_PUBLIC, C_MEMBER}


@pytest.mark.asyncio
async def test_the_limit_error_is_an_authorization_unavailable_error():
    facts = await _facts_with_classes()

    with pytest.raises(HarborAuthorizationUnavailableError):
        await facts.readable_class_ids(_access("u1"), limit=0)


@pytest.mark.asyncio
async def test_readable_classes_never_cross_tenants():
    facts = _facts()
    await facts.set_class_snapshots(
        [
            _snapshot(C_PUBLIC, tenant_id="t2", public=True),
            _snapshot(C_MEMBER, allowed=("u1",)),
        ]
    )

    assert await facts.readable_class_ids(_access("u1", "t1"), limit=10) == {C_MEMBER}
    assert await facts.readable_class_ids(_access("u1", "t2"), limit=10) == {C_PUBLIC}
    assert await facts.readable_class_ids(_access("u1", "t3"), limit=10) == frozenset()


@pytest.mark.asyncio
async def test_a_snapshot_that_is_not_yet_valid_is_not_readable():
    facts = _facts()
    await facts.set_class_snapshots(
        [
            _snapshot(
                C_PUBLIC,
                public=True,
                resolved_at=NOW + timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
            )
        ]
    )

    assert await facts.readable_class_ids(_access(), limit=10) == frozenset()


@pytest.mark.asyncio
async def test_readability_uses_the_injected_clock_not_wall_time():
    before = NOW - timedelta(hours=2)
    snapshot = _snapshot(
        C_MEMBER,
        allowed=("u1",),
        resolved_at=NOW - timedelta(hours=3),
        expires_at=NOW - timedelta(hours=1),
    )
    early, late = _facts(before), _facts(NOW)
    for facts in (early, late):
        await facts.set_class_snapshots([snapshot])

    assert await early.readable_class_ids(_access(), limit=10) == {C_MEMBER}
    assert await late.readable_class_ids(_access(), limit=10) == frozenset()


@pytest.mark.asyncio
async def test_readable_classes_are_decided_at_one_instant():
    ticks = iter([NOW, NOW + timedelta(days=365)])
    facts = InMemoryAccessFacts(clock=lambda: next(ticks))
    await facts.set_class_snapshots(
        [_snapshot(C_PUBLIC, public=True), _snapshot(C_MEMBER, allowed=("u1",))]
    )

    assert await facts.readable_class_ids(_access(), limit=10) == {C_PUBLIC, C_MEMBER}


@pytest.mark.asyncio
async def test_the_default_clock_is_wall_time():
    facts = InMemoryAccessFacts()
    now = datetime.now(UTC)
    await facts.set_class_snapshots(
        [
            _snapshot(
                C_PUBLIC,
                public=True,
                resolved_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(minutes=5),
            ),
            # Valid around the fixed 2031 instant only; wall time has not reached it.
            _snapshot(C_MEMBER, public=True, resolved_at=NOW - timedelta(hours=2), expires_at=NOW),
        ]
    )

    assert await facts.readable_class_ids(_access(), limit=10) == {C_PUBLIC}


# Entitlements -------------------------------------------------------------


@pytest.mark.asyncio
async def test_entitlements_follow_project_memberships():
    facts = _facts()
    assert await facts.entitled_source_scopes("t1", "u1") is None

    await facts.set_project_scopes("t1", "p1", frozenset({"s1", "s2"}))
    await facts.replace_project_members(
        "t1",
        "p1",
        [
            ProjectMember("p1", "u1", "editor"),
            ProjectMember("p1", "u2", "reader", frozenset({"s2"})),
        ],
    )

    assert await facts.entitled_source_scopes("t1", "u1") == {"s1", "s2"}
    assert await facts.entitled_source_scopes("t1", "u2") == {"s2"}
    assert await facts.entitled_source_scopes("t1", "u3") == frozenset()


@pytest.mark.asyncio
async def test_a_member_narrowed_to_nothing_is_entitled_to_nothing():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader", frozenset())]
    )

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()


@pytest.mark.asyncio
async def test_narrowing_to_another_projects_datasource_entitles_nothing():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.set_project_scopes("t1", "p2", frozenset({"s-secret"}))
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader", frozenset({"s-secret"}))]
    )

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()

    # Narrowing picks among the project's own datasources; the rest of it is dropped.
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader", frozenset({"s1", "s-secret"}))]
    )
    assert await facts.entitled_source_scopes("t1", "u1") == {"s1"}


@pytest.mark.asyncio
async def test_removing_a_datasource_from_a_project_revokes_it_from_narrowed_members():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1", "s2"}))
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader", frozenset({"s2"}))]
    )
    assert await facts.entitled_source_scopes("t1", "u1") == {"s2"}

    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()

    await facts.set_project_scopes("t1", "p1", frozenset())
    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()


@pytest.mark.asyncio
async def test_a_project_without_datasources_entitles_its_narrowed_members_to_nothing():
    facts = _facts()
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader", frozenset({"s1"}))]
    )

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()


@pytest.mark.asyncio
async def test_a_whole_project_member_follows_the_projects_scopes():
    facts = _facts()
    await facts.replace_project_members("t1", "p1", [ProjectMember("p1", "u1", "reader")])

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()

    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    assert await facts.entitled_source_scopes("t1", "u1") == {"s1"}

    await facts.set_project_scopes("t1", "p1", frozenset({"s1", "s3"}))
    assert await facts.entitled_source_scopes("t1", "u1") == {"s1", "s3"}


@pytest.mark.asyncio
async def test_entitlements_are_the_union_over_projects():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1", "s2"}))
    await facts.set_project_scopes("t1", "p2", frozenset({"s3", "s4"}))
    await facts.replace_project_members("t1", "p1", [ProjectMember("p1", "u1", "reader")])
    await facts.replace_project_members(
        "t1", "p2", [ProjectMember("p2", "u1", "reader", frozenset({"s4"}))]
    )

    assert await facts.entitled_source_scopes("t1", "u1") == {"s1", "s2", "s4"}


@pytest.mark.asyncio
async def test_replacing_a_projects_members_drops_the_previous_ones():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.replace_project_members(
        "t1", "p1", [ProjectMember("p1", "u1", "reader"), ProjectMember("p1", "u2", "reader")]
    )
    await facts.replace_project_members("t1", "p1", [ProjectMember("p1", "u2", "reader")])

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()
    assert await facts.entitled_source_scopes("t1", "u2") == {"s1"}


@pytest.mark.asyncio
async def test_a_tenant_that_never_configured_rbac_has_none():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.replace_project_members("t2", "p1", [ProjectMember("p1", "u1", "reader")])

    assert await facts.entitled_source_scopes("t1", "u1") is None
    assert await facts.entitled_source_scopes("t3", "u1") is None


@pytest.mark.asyncio
async def test_removing_every_member_keeps_rbac_on():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.replace_project_members("t1", "p1", [ProjectMember("p1", "u1", "reader")])
    await facts.replace_project_members("t1", "p2", [ProjectMember("p2", "u2", "reader")])
    assert await facts.entitled_source_scopes("t1", "u1") == {"s1"}

    await facts.replace_project_members("t1", "p1", [])
    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()

    await facts.replace_project_members("t1", "p2", [])
    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()
    assert await facts.entitled_source_scopes("t1", "u2") == frozenset()
    assert await facts.entitled_source_scopes("t1", "u3") == frozenset()


@pytest.mark.asyncio
async def test_an_empty_first_push_enables_rbac():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    assert await facts.entitled_source_scopes("t1", "u1") is None

    await facts.replace_project_members("t1", "p1", [])

    assert await facts.entitled_source_scopes("t1", "u1") == frozenset()
    assert await facts.entitled_source_scopes("t1", "anyone") == frozenset()
    assert await facts.entitled_source_scopes("t2", "u1") is None


@pytest.mark.asyncio
async def test_entitlements_never_cross_tenants():
    facts = _facts()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    await facts.replace_project_members("t1", "p1", [ProjectMember("p1", "u1", "reader")])

    assert await facts.entitled_source_scopes("t2", "u1") is None
    await facts.replace_project_members("t2", "p9", [ProjectMember("p9", "u9", "reader")])
    assert await facts.entitled_source_scopes("t2", "u1") == frozenset()
    assert await facts.entitled_source_scopes("t1", "u9") == frozenset()


@pytest.mark.asyncio
async def test_a_project_with_no_scopes_set_has_an_empty_set():
    facts = _facts()

    assert await facts.project_scopes("t1", "p1") == frozenset()
    await facts.set_project_scopes("t1", "p1", frozenset({"s1"}))
    assert await facts.project_scopes("t1", "p1") == {"s1"}
    assert await facts.project_scopes("t1", "p2") == frozenset()
    assert await facts.project_scopes("t2", "p1") == frozenset()


@pytest.mark.asyncio
async def test_members_of_another_project_are_refused():
    facts = _facts()

    with pytest.raises(ValueError, match="project"):
        await facts.replace_project_members("t1", "p1", [ProjectMember("p2", "u1", "reader")])

    assert await facts.entitled_source_scopes("t1", "u1") is None


# Directory and identity links ---------------------------------------------


@pytest.mark.asyncio
async def test_a_replaced_directory_is_read_back_by_tenant_and_realm():
    facts = _facts()
    snapshot = _directory()
    await facts.replace_directory(snapshot)

    assert await facts.directory("t1", REALM) == snapshot
    assert await facts.directory("t1", OTHER_REALM) is None
    assert await facts.directory("t2", REALM) is None


@pytest.mark.asyncio
async def test_replacing_a_directory_discards_the_previous_one_for_that_realm():
    facts = _facts()
    await facts.replace_directory(_directory())
    newer = _directory(synced_at=NOW + timedelta(hours=1), active_users=frozenset())
    await facts.replace_directory(newer)

    assert await facts.directory("t1", REALM) == newer


@pytest.mark.asyncio
async def test_realm_sync_times_list_every_realm_of_the_tenant():
    facts = _facts()
    later = NOW + timedelta(hours=1)
    await facts.replace_directory(_directory())
    await facts.replace_directory(
        _directory(
            realm=OTHER_REALM,
            synced_at=later,
            members={},
            scope_grants={},
            active_users=frozenset(),
        )
    )
    await facts.replace_directory(_directory(tenant_id="t2", synced_at=later))

    assert await facts.realm_sync_times("t1") == {REALM: NOW, OTHER_REALM: later}
    assert await facts.realm_sync_times("t3") == {}


@pytest.mark.asyncio
async def test_identity_links_are_replaced_whole_per_tenant():
    facts = _facts()
    first = IdentityLink("u1", _user("a1"))
    second = IdentityLink("u2", _user("a2"))
    other = IdentityLink("u1", _user("a9", OTHER_REALM))
    assert await facts.links("t1") == ()

    await facts.replace_links("t1", [first, second])
    await facts.replace_links("t2", [other])
    assert await facts.links("t1") == (first, second)
    assert await facts.links("t2") == (other,)

    await facts.replace_links("t1", [second])
    assert await facts.links("t1") == (second,)


@pytest.mark.asyncio
async def test_the_directory_returned_to_a_caller_cannot_be_used_to_change_the_store():
    facts = _facts()
    await facts.replace_directory(_directory())
    stored = await facts.directory("t1", REALM)

    assert stored is not None
    with pytest.raises(TypeError):
        stored.members[_group("g9")] = frozenset()  # type: ignore[index]
    assert (await facts.directory("t1", REALM)) == _directory()


# Decision log -------------------------------------------------------------


def _entry(
    resource_id: str, *, allowed: bool = True, actor_id: str | None = None
) -> DecisionLogEntry:
    return DecisionLogEntry(
        tenant_id="t1",
        principal_id="u1",
        actor_id=actor_id,
        resource=ResourceRef("document", resource_id),
        decision=Decision(allowed, "member" if allowed else "not_member", C_MEMBER),
        at=NOW,
    )


@pytest.mark.asyncio
async def test_recorded_decisions_are_kept_in_order():
    facts = _facts()
    assert facts.decisions == []

    first, second, third = _entry("d1"), _entry("d2", allowed=False), _entry("d3", actor_id="a1")
    await facts.record([first, second])
    await facts.record([third])

    assert facts.decisions == [first, second, third]


@pytest.mark.asyncio
async def test_an_empty_record_call_adds_nothing():
    facts = _facts()
    await facts.record([])

    assert facts.decisions == []


# Port conformance ---------------------------------------------------------


@pytest.mark.parametrize("port", [AccessClassStore, DirectoryStore, EntitlementStore, DecisionLog])
def test_every_port_method_is_async_on_the_fake_and_on_the_combined_port(port: type):
    names = {n for n, member in vars(port).items() if inspect.iscoroutinefunction(member)}

    assert names
    for name in names:
        assert inspect.iscoroutinefunction(getattr(InMemoryAccessFacts, name)), name
        assert inspect.iscoroutinefunction(getattr(AccessFactsPort, name)), name
        assert inspect.signature(getattr(InMemoryAccessFacts, name)).parameters.keys() == (
            inspect.signature(getattr(port, name)).parameters.keys()
        ), name


# Directory model ----------------------------------------------------------


def test_a_directory_keeps_what_it_was_given():
    snapshot = _directory(public_scopes=frozenset({"confluence-space:PUB"}))

    assert snapshot.members == {_group("g1"): frozenset({_user("a1"), _group("g2")})}
    assert snapshot.scope_grants == {SCOPE: frozenset({_group("g1")})}
    assert snapshot.active_users == {_user("a1")}
    assert snapshot.public_scopes == {"confluence-space:PUB"}
    assert _directory().public_scopes == frozenset()


def test_a_directory_copies_its_inputs_so_later_changes_do_not_leak_in():
    members = {_group("g1"): {_user("a1")}}
    snapshot = _directory(members=members)
    members[_group("g1")].add(_user("a2"))
    members[_group("g2")] = {_user("a3")}

    assert snapshot.members == {_group("g1"): frozenset({_user("a1")})}
    assert isinstance(snapshot.members[_group("g1")], frozenset)


@pytest.mark.parametrize(
    "overrides",
    [
        {"members": {_group("g1", OTHER_REALM): frozenset({_user("a1")})}},
        {"members": {_group("g1"): frozenset({_user("a1", OTHER_REALM)})}},
        {"scope_grants": {SCOPE: frozenset({_group("g1", OTHER_REALM)})}},
        {"active_users": frozenset({_user("a1", OTHER_REALM)})},
    ],
    ids=["member-group", "group-member", "scope-grant", "active-user"],
)
def test_a_directory_rejects_principals_from_another_realm(overrides: dict[str, object]):
    with pytest.raises(ValueError, match="realm"):
        _directory(**overrides)


def test_a_directory_realm_must_match_exactly():
    with pytest.raises(ValueError, match="realm"):
        _directory(active_users=frozenset({_user("a1", REALM + ".evil")}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": " "},
        {"realm": ""},
        {"synced_at": datetime(2031, 1, 1)},
        {"members": {"g1": frozenset({_user("a1")})}},
        {"members": {_group("g1"): frozenset({"a1"})}},
        {"scope_grants": {" ": frozenset({_group("g1")})}},
        {"active_users": frozenset({"a1"})},
        {"public_scopes": frozenset({""})},
    ],
    ids=[
        "tenant",
        "realm",
        "naive-time",
        "member-key",
        "member-value",
        "blank-scope",
        "user-type",
        "blank-public-scope",
    ],
)
def test_a_malformed_directory_is_rejected(overrides: dict[str, object]):
    with pytest.raises(ValueError):
        _directory(**overrides)


def test_a_directory_is_frozen():
    snapshot = _directory()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.realm = OTHER_REALM  # type: ignore[misc]


def test_an_identity_link_names_a_user_account():
    link = IdentityLink("u1", _user("a1"))

    assert (link.principal_id, link.account) == ("u1", _user("a1"))


@pytest.mark.parametrize("kind", ["group", "role", "level"])
def test_an_identity_link_must_name_a_user_account(kind: str):
    with pytest.raises(ValueError, match="user"):
        IdentityLink("u1", SourcePrincipal(realm=REALM, kind=kind, id="x1"))


def test_an_identity_link_needs_a_principal_and_a_source_principal():
    with pytest.raises(ValueError, match="principal_id"):
        IdentityLink(" ", _user("a1"))
    with pytest.raises(ValueError, match="account"):
        IdentityLink("u1", "a1")  # type: ignore[arg-type]


def test_an_identity_link_principal_id_is_bounded_like_an_access_context_principal_id():
    # AccessContext.principal_id allows 1..255 characters; a longer id could never be a reader.
    assert IdentityLink("u" * 255, _user("a1")).principal_id == "u" * 255
    with pytest.raises(ValueError, match="255"):
        IdentityLink("u" * 256, _user("a1"))


def test_a_project_member_defaults_to_every_datasource_of_its_project():
    everything = ProjectMember("p1", "u1", "reader")
    narrowed = ProjectMember("p1", "u1", "reader", {"s1"})  # type: ignore[arg-type]
    nothing = ProjectMember("p1", "u1", "reader", frozenset())

    assert everything.source_scope_ids is None
    assert narrowed.source_scope_ids == frozenset({"s1"})
    assert isinstance(narrowed.source_scope_ids, frozenset)
    assert nothing.source_scope_ids == frozenset()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project_id": " "},
        {"principal_id": ""},
        {"role": "superuser"},
        {"source_scope_ids": frozenset({" "})},
        {"source_scope_ids": "s1"},
    ],
    ids=["project", "principal", "role", "scope", "bare-string-scope"],
)
def test_a_malformed_project_member_is_rejected(kwargs: dict[str, object]):
    values: dict[str, object] = {"project_id": "p1", "principal_id": "u1", "role": "reader"}
    values.update(kwargs)

    with pytest.raises(ValueError):
        ProjectMember(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "reader"])
def test_every_member_role_is_accepted(role: str):
    assert ProjectMember("p1", "u1", role).role == role  # type: ignore[arg-type]


def test_a_decision_log_entry_keeps_what_it_was_given():
    entry = _entry("d1", actor_id="a1")

    assert (entry.tenant_id, entry.principal_id, entry.actor_id, entry.at) == (
        "t1",
        "u1",
        "a1",
        NOW,
    )
    assert entry.resource == ResourceRef("document", "d1")
    assert entry.decision == Decision(True, "member", C_MEMBER)
    assert _entry("d2").actor_id is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tenant_id": " "}, "tenant_id"),
        ({"principal_id": ""}, "principal_id"),
        ({"actor_id": " "}, "actor_id"),
        ({"resource": {"kind": "document", "id": "d1"}}, "resource must"),
        ({"decision": True}, "decision must"),
        ({"at": datetime(2031, 1, 1)}, "entry at"),
    ],
    ids=["tenant", "principal", "actor", "resource", "decision", "naive-time"],
)
def test_a_malformed_decision_log_entry_is_rejected(changes: dict[str, object], message: str):
    with pytest.raises(ValueError, match=message):
        dataclasses.replace(_entry("d1"), **changes)


def test_a_change_batch_carries_item_keys_and_an_aware_cursor():
    batch = ChangeBatch(item_keys=("k1", "k2"), directory_changed=True, cursor=NOW)

    assert (batch.item_keys, batch.directory_changed, batch.cursor) == (("k1", "k2"), True, NOW)
    assert ChangeBatch(["k1"], False, NOW).item_keys == ("k1",)  # type: ignore[arg-type]


def test_a_malformed_change_batch_is_rejected():
    with pytest.raises(ValueError, match="cursor"):
        ChangeBatch(item_keys=(), directory_changed=False, cursor=datetime(2031, 1, 1))
    with pytest.raises(ValueError, match="item key"):
        ChangeBatch(item_keys=("k1", " "), directory_changed=False, cursor=NOW)
    with pytest.raises(ValueError, match="item key"):
        ChangeBatch(item_keys="k1", directory_changed=False, cursor=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="directory_changed"):
        ChangeBatch(item_keys=(), directory_changed=1, cursor=NOW)  # type: ignore[arg-type]


# Verified tokens ----------------------------------------------------------


def test_a_verified_token_defaults_to_a_jwt_without_an_actor():
    token = VerifiedToken(
        subject="sub-1", user_id="u1", role="reader", tenant_ids=frozenset({"t1"})
    )

    assert (token.token_kind, token.actor_id) == ("jwt", None)
    assert token.tenant_ids == {"t1"}


def test_a_verified_token_freezes_its_tenants():
    token = VerifiedToken(
        subject="sub-1",
        user_id="u1",
        role="admin",
        tenant_ids={"t1", "t2"},  # type: ignore[arg-type]
        actor_id="a1",
        token_kind="api_key",
    )

    assert isinstance(token.tenant_ids, frozenset)
    assert (token.actor_id, token.token_kind) == ("a1", "api_key")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject": " "},
        {"user_id": ""},
        {"role": ""},
        {"tenant_ids": frozenset({" "})},
        {"tenant_ids": "t1"},
        {"actor_id": ""},
        {"token_kind": " "},
    ],
    ids=["subject", "user", "role", "tenant", "bare-string-tenants", "actor", "kind"],
)
def test_a_malformed_verified_token_is_rejected(kwargs: dict[str, object]):
    values: dict[str, object] = {
        "subject": "sub-1",
        "user_id": "u1",
        "role": "reader",
        "tenant_ids": frozenset({"t1"}),
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        VerifiedToken(**values)  # type: ignore[arg-type]
