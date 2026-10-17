from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.classes import AclClass
from harborrag_core.security.context import AccessContext
from harborrag_core.security.directory import DirectorySnapshot, IdentityLink
from harborrag_core.sources.acl import SourceAcl, SourcePrincipal
from harborrag_engine.security.class_readers import ClassReaders, class_readers, class_snapshot
from harborrag_engine.security.fingerprint import class_of
from harborrag_engine.security.resolver import account_index, expand

REALM = "atlassian:acme.atlassian.net"
OTHER_REALM = "atlassian:other.atlassian.net"
SCOPE = "jira-project:CPM"
TENANT = "tenant-1"
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def principal(kind: str, identifier: str, *, realm: str = REALM) -> SourcePrincipal:
    return SourcePrincipal(realm=realm, kind=kind, id=identifier)


def user(identifier: str, *, realm: str = REALM) -> SourcePrincipal:
    return principal("user", identifier, realm=realm)


def group(identifier: str, *, realm: str = REALM) -> SourcePrincipal:
    return principal("group", identifier, realm=realm)


def directory(  # noqa: PLR0913 - one keyword per directory field a case pins
    *,
    members: Mapping[SourcePrincipal, Iterable[SourcePrincipal]] | None = None,
    grants: Mapping[str, Iterable[SourcePrincipal]] | None = None,
    active: Iterable[SourcePrincipal] = (),
    public: Iterable[str] = (),
    realm: str = REALM,
    tenant_id: str = TENANT,
) -> DirectorySnapshot:
    return DirectorySnapshot(
        tenant_id=tenant_id,
        realm=realm,
        synced_at=T0,
        members={key: frozenset(value) for key, value in (members or {}).items()},
        scope_grants={key: frozenset(value) for key, value in (grants or {}).items()},
        active_users=frozenset(active),
        public_scopes=frozenset(public),
    )


def accounts(**links: SourcePrincipal) -> dict[SourcePrincipal, frozenset[str]]:
    """``accounts(hr_alice=user("a1"))`` links HarborRAG principal ``hr_alice`` to ``user:a1``."""

    return dict(
        account_index(
            IdentityLink(principal_id=name.replace("_", "-"), account=account)
            for name, account in links.items()
        )
    )


def inherited_class(scope: str = SCOPE, *, realm: str = REALM) -> AclClass:
    return class_of(TENANT, SourceAcl.inherited(realm=realm, scope=scope))


def restricted_class(
    levels: list[list[SourcePrincipal]], scope: str = SCOPE, *, realm: str = REALM
) -> AclClass:
    acl = SourceAcl.restricted(realm=realm, scope=scope, levels=levels, reason="restricted")
    return class_of(TENANT, acl)


# --- account_index ---------------------------------------------------------------------------


def test_account_index_groups_principal_ids_by_account() -> None:
    index = account_index(
        [
            IdentityLink(principal_id="hr-alice", account=user("a1")),
            IdentityLink(principal_id="alice-sso", account=user("a1")),
            IdentityLink(principal_id="hr-bob", account=user("b1")),
        ]
    )

    assert index == {
        user("a1"): frozenset({"hr-alice", "alice-sso"}),
        user("b1"): frozenset({"hr-bob"}),
    }
    assert account_index([]) == {}


def test_account_index_looks_up_an_account_by_identity_and_not_by_display_name() -> None:
    labelled = SourcePrincipal(realm=REALM, kind="user", id="a1", display_name="Alice A.")
    index = account_index([IdentityLink(principal_id="hr-alice", account=labelled)])

    assert index[user("a1")] == frozenset({"hr-alice"})
    assert user("a2") not in index


def test_account_index_is_read_only() -> None:
    index = account_index([IdentityLink(principal_id="hr-alice", account=user("a1"))])

    with pytest.raises(TypeError):
        index[user("a2")] = frozenset({"x"})  # type: ignore[index]


# --- expand ----------------------------------------------------------------------------------


def test_expand_gives_an_active_user_itself_and_drops_an_inactive_one() -> None:
    snapshot = directory(active=[user("a1")])

    assert expand(user("a1"), snapshot) == (frozenset({user("a1")}), frozenset())
    assert expand(user("gone"), snapshot) == (frozenset(), frozenset())


def test_expand_everyone_is_every_active_user() -> None:
    snapshot = directory(active=[user("a1"), user("a2")])

    users, unresolved = expand(principal("everyone", "*"), snapshot)

    assert users == {user("a1"), user("a2")}
    assert unresolved == frozenset()


def test_expand_follows_nested_groups_down_to_active_users() -> None:
    snapshot = directory(
        members={
            group("outer"): [group("inner"), user("a1")],
            group("inner"): [user("a2"), user("gone")],
        },
        active=[user("a1"), user("a2")],
    )

    assert expand(group("outer"), snapshot) == (frozenset({user("a1"), user("a2")}), frozenset())


def test_a_group_cycle_terminates_and_still_yields_every_member() -> None:
    snapshot = directory(
        members={
            group("g1"): [group("g2"), user("a1")],
            group("g2"): [group("g1"), user("a2")],
            group("self"): [group("self"), user("a1")],
        },
        active=[user("a1"), user("a2")],
    )

    assert expand(group("g1"), snapshot) == (frozenset({user("a1"), user("a2")}), frozenset())
    assert expand(group("g2"), snapshot) == (frozenset({user("a1"), user("a2")}), frozenset())
    assert expand(group("self"), snapshot) == (frozenset({user("a1")}), frozenset())


def test_a_very_deep_group_chain_does_not_recurse() -> None:
    depth = 5_000  # well past the interpreter's default recursion limit of 1,000
    members: dict[SourcePrincipal, frozenset[SourcePrincipal]] = {
        group(f"g{level}"): frozenset({group(f"g{level + 1}")}) for level in range(depth - 1)
    }
    members[group(f"g{depth - 1}")] = frozenset({user("deep")})
    snapshot = directory(members=members, active=[user("deep")])

    assert expand(group("g0"), snapshot) == (frozenset({user("deep")}), frozenset())


def test_a_deep_chain_closing_into_a_cycle_terminates() -> None:
    depth = 5_000
    members: dict[SourcePrincipal, frozenset[SourcePrincipal]] = {
        group(f"g{level}"): frozenset({group(f"g{(level + 1) % depth}")}) for level in range(depth)
    }
    members[group("g10")] = frozenset({group("g11"), user("a1")})
    snapshot = directory(members=members, active=[user("a1")])

    assert expand(group("g0"), snapshot) == (frozenset({user("a1")}), frozenset())


def test_expand_reports_what_the_directory_cannot_resolve() -> None:
    snapshot = directory(
        members={group("known"): [group("ghost"), user("a1")]},
        active=[user("a1")],
    )

    users, unresolved = expand(group("known"), snapshot)

    assert users == {user("a1")}
    assert unresolved == {group("ghost")}
    assert expand(group("ghost"), snapshot) == (frozenset(), frozenset({group("ghost")}))


def test_expand_reports_a_principal_of_another_realm_even_when_it_is_a_user() -> None:
    snapshot = directory(active=[user("a1")], members={group("g"): [user("a1")]})

    foreign_user = user("a1", realm=OTHER_REALM)
    foreign_group = group("g", realm=OTHER_REALM)

    assert expand(foreign_user, snapshot) == (frozenset(), frozenset({foreign_user}))
    assert expand(foreign_group, snapshot) == (frozenset(), frozenset({foreign_group}))
    assert expand(principal("everyone", "*", realm=OTHER_REALM), snapshot)[0] == frozenset()


def test_expand_never_resolves_an_issue_role_even_if_the_directory_lists_one() -> None:
    reporter = principal("jira_issue_role", "reporter")
    snapshot = directory(members={reporter: [user("a1")]}, active=[user("a1")])

    assert expand(reporter, snapshot) == (frozenset(), frozenset({reporter}))


def test_expand_does_not_treat_a_site_local_user_as_a_user() -> None:
    site_user = principal("site_user", "https://x.sharepoint.com/sites/a/7")
    snapshot = directory(active=[user("a1")])

    assert expand(site_user, snapshot) == (frozenset(), frozenset({site_user}))


# --- class_readers ---------------------------------------------------------------------------


def test_inherited_class_reads_through_the_scope_grants() -> None:
    snapshot = directory(
        members={group("recruiting"): [user("a1"), user("a2")]},
        grants={SCOPE: [group("recruiting")]},
        active=[user("a1"), user("a2")],
    )
    links = accounts(hr_alice=user("a1"))

    readers = class_readers(inherited_class(), snapshot, links)

    assert readers.principal_ids == {"hr-alice"}
    assert readers.public is False
    assert readers.scope_known is True
    assert readers.unresolved == {user("a2")}  # a2 can read at the source but has no link


def test_a_reader_with_several_linked_principals_yields_all_of_them() -> None:
    snapshot = directory(grants={SCOPE: [user("a1")]}, active=[user("a1")])
    links = accounts(hr_alice=user("a1"), alice_sso=user("a1"))

    readers = class_readers(inherited_class(), snapshot, links)

    assert readers.principal_ids == {"hr-alice", "alice-sso"}
    assert readers.unresolved == frozenset()


def test_an_inactive_user_in_the_scope_grants_reads_nothing() -> None:
    snapshot = directory(grants={SCOPE: [user("a1"), user("left")]}, active=[user("a1")])
    links = accounts(hr_alice=user("a1"), hr_leaver=user("left"))

    readers = class_readers(inherited_class(), snapshot, links)

    assert readers.principal_ids == {"hr-alice"}


def test_restricted_class_is_scope_intersected_with_every_level() -> None:
    snapshot = directory(
        members={
            group("engineers"): [user("a1"), user("a2"), user("a3")],
            group("leads"): [user("a2"), user("a3"), user("a4")],
        },
        grants={SCOPE: [group("engineers")]},
        active=[user("a1"), user("a2"), user("a3"), user("a4")],
    )
    links = accounts(
        p_a1=user("a1"), p_a2=user("a2"), p_a3=user("a3"), p_a4=user("a4"), p_a5=user("a5")
    )
    acl_class = restricted_class([[user("a1"), user("a2"), user("a4")], [group("leads")]])

    readers = class_readers(acl_class, snapshot, links)

    # a1 misses the second level and a3 the first. a4 passes both levels and is stopped
    # by the scope alone: it is not one of the engineers.
    assert readers.principal_ids == {"p-a2"}
    assert readers.public is False
    assert readers.scope_known is True


def test_a_restricted_class_never_reads_beyond_the_scope_readers() -> None:
    # a2 is active and linked and named by the level, but the scope grants only a1.
    snapshot = directory(grants={SCOPE: [user("a1")]}, active=[user("a1"), user("a2")])
    links = accounts(p_a1=user("a1"), p_a2=user("a2"))

    readers = class_readers(restricted_class([[user("a1"), user("a2")]]), snapshot, links)

    assert readers.principal_ids == {"p-a1"}
    assert readers.scope_known is True


def test_an_everyone_level_is_still_bounded_by_the_scope_readers() -> None:
    snapshot = directory(
        members={group("staff"): [user("a1"), user("a2")]},
        grants={SCOPE: [user("a1")]},
        active=[user("a1"), user("a2"), user("a3")],
    )
    links = accounts(p_a1=user("a1"), p_a2=user("a2"), p_a3=user("a3"))
    everyone = principal("everyone", "*")

    only_everyone = class_readers(restricted_class([[everyone]]), snapshot, links)
    everyone_and_staff = class_readers(
        restricted_class([[everyone], [group("staff")]]), snapshot, links
    )

    assert only_everyone.principal_ids == {"p-a1"}
    assert everyone_and_staff.principal_ids == {"p-a1"}


def test_a_scope_granted_to_nobody_leaves_a_restricted_class_with_no_readers() -> None:
    snapshot = directory(grants={SCOPE: []}, active=[user("a1"), user("a2")])
    links = accounts(p_a1=user("a1"), p_a2=user("a2"))
    acl_class = restricted_class([[user("a1"), user("a2"), principal("everyone", "*")]])

    readers = class_readers(acl_class, snapshot, links)

    assert readers.principal_ids == frozenset()
    assert readers.scope_known is True


def test_restricted_levels_of_one_group_each_all_must_match() -> None:
    snapshot = directory(
        members={group("x"): [user("a1"), user("a2")], group("y"): [user("a2"), user("a3")]},
        grants={SCOPE: [user("a1"), user("a2"), user("a3")]},
        active=[user("a1"), user("a2"), user("a3")],
    )
    links = accounts(p_a1=user("a1"), p_a2=user("a2"), p_a3=user("a3"))

    one_level = class_readers(restricted_class([[group("x"), group("y")]]), snapshot, links)
    two_levels = class_readers(restricted_class([[group("x")], [group("y")]]), snapshot, links)

    assert one_level.principal_ids == {"p-a1", "p-a2", "p-a3"}
    assert two_levels.principal_ids == {"p-a2"}


def test_a_level_that_resolves_to_nobody_leaves_the_class_with_no_readers() -> None:
    snapshot = directory(
        members={group("staff"): [user("a1")]},
        grants={SCOPE: [group("staff")]},
        active=[user("a1")],
    )
    acl_class = restricted_class([[group("staff")], [group("ghost")]])

    readers = class_readers(acl_class, snapshot, accounts(hr_alice=user("a1")))

    assert readers.principal_ids == frozenset()
    assert readers.unresolved == {group("ghost")}
    assert readers.scope_known is True


def test_nested_groups_expand_and_a_cycle_terminates() -> None:
    snapshot = directory(
        members={
            group("g1"): [group("g2"), user("a1")],
            group("g2"): [group("g1"), user("a2")],
        },
        grants={SCOPE: [group("g1")]},
        active=[user("a1"), user("a2")],
    )
    links = accounts(p_a1=user("a1"), p_a2=user("a2"))

    inherited = class_readers(inherited_class(), snapshot, links)
    restricted = class_readers(restricted_class([[group("g2")], [group("g1")]]), snapshot, links)

    assert inherited.principal_ids == {"p-a1", "p-a2"}
    assert restricted.principal_ids == {"p-a1", "p-a2"}
    assert restricted.unresolved == frozenset()


def test_a_very_deep_group_chain_still_resolves_readers() -> None:
    depth = 5_000
    members: dict[SourcePrincipal, frozenset[SourcePrincipal]] = {
        group(f"g{level}"): frozenset({group(f"g{level + 1}")}) for level in range(depth - 1)
    }
    members[group(f"g{depth - 1}")] = frozenset({user("deep")})
    snapshot = directory(members=members, grants={SCOPE: [group("g0")]}, active=[user("deep")])

    readers = class_readers(inherited_class(), snapshot, accounts(hr_deep=user("deep")))

    assert readers.principal_ids == {"hr-deep"}


def test_unknown_group_matches_nobody_and_is_reported() -> None:
    snapshot = directory(
        members={group("recruiting"): [user("a1")]},
        grants={SCOPE: [group("recruiting"), group("ghost")]},
        active=[user("a1")],
    )
    links = accounts(hr_alice=user("a1"))

    inherited = class_readers(inherited_class(), snapshot, links)
    only_ghost = class_readers(restricted_class([[group("ghost")]]), snapshot, links)

    assert inherited.principal_ids == {"hr-alice"}  # the unknown group adds nobody
    assert inherited.unresolved == {group("ghost")}
    assert only_ghost.principal_ids == frozenset()
    assert only_ghost.unresolved == {group("ghost")}


def test_a_group_nested_in_a_known_group_is_reported_when_unknown() -> None:
    snapshot = directory(
        members={group("recruiting"): [group("contractors"), user("a1")]},
        grants={SCOPE: [group("recruiting")]},
        active=[user("a1")],
    )

    readers = class_readers(inherited_class(), snapshot, accounts(hr_alice=user("a1")))

    assert readers.principal_ids == {"hr-alice"}
    assert readers.unresolved == {group("contractors")}


def test_an_unlinked_reader_is_reported_and_matches_nobody() -> None:
    snapshot = directory(grants={SCOPE: [user("a1"), user("a2")]}, active=[user("a1"), user("a2")])

    readers = class_readers(inherited_class(), snapshot, accounts())

    assert readers.principal_ids == frozenset()
    assert readers.unresolved == {user("a1"), user("a2")}
    assert readers.scope_known is True


def test_issue_role_grants_resolve_to_nobody() -> None:
    reporter = principal("jira_issue_role", "reporter")
    snapshot = directory(grants={SCOPE: [user("a1"), user("a2")]}, active=[user("a1"), user("a2")])
    links = accounts(p_a1=user("a1"), p_a2=user("a2"))

    only_role = class_readers(restricted_class([[reporter]]), snapshot, links)
    role_next_to_a_user = class_readers(restricted_class([[reporter, user("a1")]]), snapshot, links)
    role_in_a_second_level = class_readers(
        restricted_class([[user("a1"), user("a2")], [reporter]]), snapshot, links
    )

    assert only_role.principal_ids == frozenset()
    assert only_role.unresolved == {reporter}
    assert role_next_to_a_user.principal_ids == {
        "p-a1"
    }  # the role grants no one, and widens nothing
    assert role_next_to_a_user.unresolved == {reporter}
    assert role_in_a_second_level.principal_ids == frozenset()
    assert role_in_a_second_level.unresolved == {reporter}


def test_a_missing_scope_gives_no_readers() -> None:
    snapshot = directory(
        members={group("staff"): [user("a1")]},
        grants={"jira-project:OTHER": [group("staff")]},
        active=[user("a1")],
        public=["jira-project:ALSO-OTHER"],
    )
    links = accounts(hr_alice=user("a1"))

    inherited = class_readers(inherited_class(), snapshot, links)
    restricted = class_readers(restricted_class([[user("a1")]]), snapshot, links)

    for readers in (inherited, restricted):
        assert readers.scope_known is False
        assert readers.principal_ids == frozenset()
        assert readers.public is False


def test_a_scope_nobody_may_read_is_known_and_has_no_readers() -> None:
    snapshot = directory(grants={SCOPE: []}, active=[user("a1")])

    readers = class_readers(inherited_class(), snapshot, accounts(hr_alice=user("a1")))

    assert readers == ClassReaders(
        principal_ids=frozenset(), public=False, unresolved=frozenset(), scope_known=True
    )


def test_a_directory_of_another_realm_gives_no_readers() -> None:
    # The same scope string is granted in the foreign directory; only the realm check stops it.
    foreign = directory(
        grants={SCOPE: [user("a1", realm=OTHER_REALM)]},
        active=[user("a1", realm=OTHER_REALM)],
        public=[SCOPE],
        realm=OTHER_REALM,
    )
    acl_class = restricted_class([[user("a1"), group("g")]])
    links = {user("a1", realm=OTHER_REALM): frozenset({"hr-alice"}), user("a1"): frozenset({"x"})}

    readers = class_readers(acl_class, foreign, links)

    assert readers.scope_known is False
    assert readers.principal_ids == frozenset()
    assert readers.public is False
    assert readers.unresolved == {user("a1"), group("g")}
    assert class_readers(inherited_class(), foreign, links).scope_known is False


def test_a_directory_of_another_tenant_gives_no_readers() -> None:
    snapshot = directory(grants={SCOPE: [user("a1")]}, active=[user("a1")], tenant_id="tenant-2")

    readers = class_readers(inherited_class(), snapshot, accounts(hr_alice=user("a1")))

    assert readers.scope_known is False
    assert readers.principal_ids == frozenset()
    assert readers.public is False


def test_a_public_scope_is_public_only_when_inherited() -> None:
    snapshot = directory(
        members={group("leads"): [user("a2")]},
        active=[user("a1"), user("a2")],
        public=[SCOPE],
    )
    links = accounts(p_a1=user("a1"), p_a2=user("a2"), p_gone=user("gone"))

    inherited = class_readers(inherited_class(), snapshot, links)
    restricted = class_readers(restricted_class([[group("leads"), user("gone")]]), snapshot, links)

    assert inherited.public is True
    assert inherited.scope_known is True
    assert inherited.principal_ids == {
        "p-a1",
        "p-a2",
    }  # a public scope contributes every active user
    assert restricted.public is False
    assert restricted.scope_known is True
    assert restricted.principal_ids == {"p-a2"}  # still limited by the level, never by 'gone'


def test_a_public_scope_that_also_has_grants_stays_public_and_reports_their_gaps() -> None:
    snapshot = directory(grants={SCOPE: [group("ghost")]}, active=[user("a1")], public=[SCOPE])

    readers = class_readers(inherited_class(), snapshot, accounts(hr_alice=user("a1")))

    assert readers.public is True
    assert readers.principal_ids == {"hr-alice"}
    assert readers.unresolved == {group("ghost")}


def test_class_readers_leaves_its_inputs_untouched() -> None:
    snapshot = directory(
        members={group("g"): [user("a1")]}, grants={SCOPE: [group("g")]}, active=[user("a1")]
    )
    links = accounts(hr_alice=user("a1"))
    before = (dict(snapshot.members), dict(snapshot.scope_grants), dict(links))

    class_readers(inherited_class(), snapshot, links)

    assert (dict(snapshot.members), dict(snapshot.scope_grants), dict(links)) == before


# --- class_snapshot --------------------------------------------------------------------------


def _digest(class_id: str, public: bool, ids: list[str]) -> str:
    return hashlib.sha256(
        json.dumps([class_id, public, ids], separators=(",", ":")).encode()
    ).hexdigest()


def test_class_snapshot_publishes_the_readers_as_a_class_permission() -> None:
    acl_class = inherited_class()
    readers = ClassReaders(
        principal_ids=frozenset({"hr-bob", "hr-alice"}),
        public=False,
        unresolved=frozenset({user("a2")}),
        scope_known=True,
    )

    snapshot = class_snapshot(
        acl_class, readers, resolved_at=T0, expires_at=T0 + timedelta(hours=1)
    )

    assert snapshot.tenant_id == TENANT
    assert snapshot.resource_kind == "class"
    assert snapshot.resource_id == acl_class.class_id
    assert snapshot.known is True
    assert snapshot.processing_allowed is True
    assert snapshot.public is False
    assert snapshot.allowed_principal_ids == ("hr-alice", "hr-bob")
    assert snapshot.denied_principal_ids == ()
    assert snapshot.resolved_at == T0
    assert snapshot.expires_at == T0 + timedelta(hours=1)
    assert snapshot.revision == _digest(acl_class.class_id, False, ["hr-alice", "hr-bob"])


def test_class_snapshot_admits_exactly_the_readers_and_a_public_class_admits_everyone() -> None:
    acl_class = inherited_class()
    tenant = TenantId(TENANT)
    now = T0 + timedelta(minutes=5)
    restricted_snapshot = class_snapshot(
        acl_class,
        ClassReaders(frozenset({"hr-alice"}), False, frozenset(), True),
        resolved_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    public_snapshot = class_snapshot(
        acl_class,
        ClassReaders(frozenset(), True, frozenset(), True),
        resolved_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )

    alice = AccessContext(principal_id="hr-alice", tenant_id=tenant)
    bob = AccessContext(principal_id="hr-bob", tenant_id=tenant)

    assert restricted_snapshot.can_read(alice, now=now) is True
    assert restricted_snapshot.can_read(bob, now=now) is False
    assert public_snapshot.can_read(bob, now=now) is True


def test_a_known_scope_with_no_readers_is_a_known_snapshot_that_admits_nobody() -> None:
    acl_class = inherited_class()
    snapshot = class_snapshot(
        acl_class,
        ClassReaders(frozenset(), False, frozenset(), True),
        resolved_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    alice = AccessContext(principal_id="hr-alice", tenant_id=TenantId(TENANT))

    assert snapshot.known is True
    assert snapshot.processing_allowed is True
    assert snapshot.public is False
    assert snapshot.allowed_principal_ids == ()
    assert snapshot.revision == _digest(acl_class.class_id, False, [])
    assert snapshot.can_read(alice, now=T0 + timedelta(minutes=1)) is False


def test_a_class_on_an_unknown_scope_is_an_unknown_snapshot_that_admits_nobody() -> None:
    acl_class = inherited_class()
    snapshot_dir = directory(grants={"jira-project:OTHER": [user("a1")]}, active=[user("a1")])
    readers = class_readers(acl_class, snapshot_dir, accounts(hr_alice=user("a1")))
    window = {"resolved_at": T0, "expires_at": T0 + timedelta(hours=1)}

    snapshot = class_snapshot(acl_class, readers, **window)
    known_but_empty = class_snapshot(
        acl_class, ClassReaders(frozenset(), False, frozenset(), True), **window
    )
    alice = AccessContext(principal_id="hr-alice", tenant_id=TenantId(TENANT))

    assert readers.scope_known is False
    assert snapshot.known is False
    assert snapshot.processing_allowed is False
    assert snapshot.public is False
    assert snapshot.allowed_principal_ids == ()
    assert snapshot.can_read(alice, now=T0 + timedelta(minutes=1)) is False
    assert snapshot.revision != known_but_empty.revision
    assert snapshot.tenant_id == TENANT
    assert snapshot.resource_kind == "class"
    assert snapshot.resource_id == acl_class.class_id


def test_an_unknown_scope_withholds_even_readers_that_were_built_by_hand() -> None:
    readers = ClassReaders(frozenset({"hr-alice"}), True, frozenset(), False)

    snapshot = class_snapshot(
        inherited_class(), readers, resolved_at=T0, expires_at=T0 + timedelta(hours=1)
    )
    alice = AccessContext(principal_id="hr-alice", tenant_id=TenantId(TENANT))

    assert snapshot.known is False
    assert snapshot.processing_allowed is False
    assert snapshot.public is False
    assert snapshot.allowed_principal_ids == ()
    assert snapshot.can_read(alice, now=T0 + timedelta(minutes=1)) is False


def test_a_public_class_lists_no_principal_ids_because_it_admits_everyone() -> None:
    acl_class = inherited_class()
    readers = ClassReaders(frozenset({"hr-alice", "hr-bob"}), True, frozenset(), True)

    snapshot = class_snapshot(
        acl_class, readers, resolved_at=T0, expires_at=T0 + timedelta(hours=1)
    )
    stranger = AccessContext(principal_id="hr-stranger", tenant_id=TenantId(TENANT))

    assert snapshot.public is True
    assert snapshot.known is True
    assert snapshot.processing_allowed is True
    assert snapshot.allowed_principal_ids == ()
    assert snapshot.revision == _digest(acl_class.class_id, True, [])
    assert snapshot.can_read(stranger, now=T0 + timedelta(minutes=1)) is True


def test_a_public_class_revision_changes_with_visibility_but_not_with_linked_users() -> None:
    window = {"resolved_at": T0, "expires_at": T0 + timedelta(hours=1)}
    acl_class = inherited_class()

    public_alice = class_snapshot(
        acl_class, ClassReaders(frozenset({"hr-alice"}), True, frozenset(), True), **window
    )
    public_bob = class_snapshot(
        acl_class, ClassReaders(frozenset({"hr-bob"}), True, frozenset(), True), **window
    )
    private_alice = class_snapshot(
        acl_class, ClassReaders(frozenset({"hr-alice"}), False, frozenset(), True), **window
    )
    private_none = class_snapshot(
        acl_class, ClassReaders(frozenset(), False, frozenset(), True), **window
    )

    assert public_alice.revision == public_bob.revision
    assert public_alice.revision != private_alice.revision
    assert public_alice.revision != private_none.revision


def test_a_public_class_with_more_readers_than_a_snapshot_can_list_is_still_published() -> None:
    readers = ClassReaders(
        frozenset(f"hr-{number}" for number in range(10_001)), True, frozenset(), True
    )

    snapshot = class_snapshot(
        inherited_class(), readers, resolved_at=T0, expires_at=T0 + timedelta(hours=1)
    )

    assert snapshot.public is True
    assert snapshot.allowed_principal_ids == ()


def test_a_restricted_class_keeps_every_reader_up_to_the_snapshot_limit() -> None:
    ids = frozenset(f"hr-{number}" for number in range(10_000))

    snapshot = class_snapshot(
        inherited_class(),
        ClassReaders(ids, False, frozenset(), True),
        resolved_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )

    assert len(snapshot.allowed_principal_ids) == 10_000
    assert set(snapshot.allowed_principal_ids) == ids


def test_a_restricted_class_over_the_snapshot_limit_raises_and_never_truncates() -> None:
    ids = frozenset(f"hr-{number}" for number in range(10_001))

    with pytest.raises(ValueError):
        class_snapshot(
            inherited_class(),
            ClassReaders(ids, False, frozenset(), True),
            resolved_at=T0,
            expires_at=T0 + timedelta(hours=1),
        )


def test_snapshot_revision_is_stable_until_readers_change() -> None:
    snapshot_dir = directory(
        grants={SCOPE: [user("a1"), user("a2")]}, active=[user("a1"), user("a2")]
    )
    acl_class = inherited_class()
    one_link = accounts(hr_alice=user("a1"))
    two_links = accounts(hr_alice=user("a1"), hr_bob=user("a2"))

    def revision(links: dict[SourcePrincipal, frozenset[str]], at: datetime) -> str:
        readers = class_readers(acl_class, snapshot_dir, links)
        return class_snapshot(
            acl_class, readers, resolved_at=at, expires_at=at + timedelta(hours=1)
        ).revision

    assert revision(one_link, T0) == revision(one_link, T0 + timedelta(days=1))
    assert revision(one_link, T0) != revision(two_links, T0)
    assert revision(two_links, T0) == revision(dict(reversed(list(two_links.items()))), T0)


def test_snapshot_revision_depends_on_the_class_the_visibility_and_the_ids() -> None:
    window = {"resolved_at": T0, "expires_at": T0 + timedelta(hours=1)}
    first, second = inherited_class(), inherited_class("jira-project:OPS")
    same = ClassReaders(frozenset({"hr-alice"}), False, frozenset(), True)

    base = class_snapshot(first, same, **window).revision

    assert class_snapshot(first, same, **window).revision == base
    assert class_snapshot(second, same, **window).revision != base
    assert (
        class_snapshot(
            first, ClassReaders(frozenset({"hr-alice"}), True, frozenset(), True), **window
        ).revision
        != base
    )
    assert (
        class_snapshot(
            first, ClassReaders(frozenset({"hr-bob"}), False, frozenset(), True), **window
        ).revision
        != base
    )
    # What is merely reported does not change who may read.
    assert (
        class_snapshot(
            first,
            ClassReaders(frozenset({"hr-alice"}), False, frozenset({user("z")}), True),
            **window,
        ).revision
        == base
    )


def test_class_snapshot_rejects_an_expiry_that_is_not_after_resolution() -> None:
    readers = ClassReaders(frozenset({"hr-alice"}), False, frozenset(), True)

    with pytest.raises(ValueError, match="expires"):
        class_snapshot(inherited_class(), readers, resolved_at=T0, expires_at=T0)
