from __future__ import annotations

import hashlib
import json

import pytest

from harborrag_core.security.classes import AclClass, ClassId
from harborrag_core.sources.acl import SourceAcl, SourcePrincipal
from harborrag_engine.security.fingerprint import acl_fingerprint, canonical_acl, class_of

REALM = "atlassian:acme.atlassian.net"
OTHER_REALM = "atlassian:other.atlassian.net"
SCOPE = "jira-project:CPM"


def user(
    identifier: str, *, display_name: str | None = None, realm: str = REALM
) -> SourcePrincipal:
    return SourcePrincipal(realm=realm, kind="user", id=identifier, display_name=display_name)


def group(identifier: str, *, display_name: str | None = None) -> SourcePrincipal:
    return SourcePrincipal(realm=REALM, kind="group", id=identifier, display_name=display_name)


def restricted(
    levels: list[list[SourcePrincipal]],
    *,
    reason: str = "page_restrictions",
    realm: str = REALM,
    scope: str = SCOPE,
) -> SourceAcl:
    return SourceAcl.restricted(realm=realm, scope=scope, levels=levels, reason=reason)


def test_canonical_form_is_sorted_keys_per_level_without_reason_or_names() -> None:
    acl = restricted(
        [[user("b"), group("g", display_name="Staff"), user("a")], [user("z")]],
        reason="anything",
    )

    assert canonical_acl(acl) == {
        "realm": REALM,
        "scope": SCOPE,
        "status": "restricted",
        "levels": [
            [f"{REALM}|group:g", f"{REALM}|user:a", f"{REALM}|user:b"],
            [f"{REALM}|user:z"],
        ],
    }


def test_canonical_form_of_an_inherited_acl_has_no_levels() -> None:
    assert canonical_acl(SourceAcl.inherited(realm=REALM, scope=SCOPE)) == {
        "realm": REALM,
        "scope": SCOPE,
        "status": "inherited",
        "levels": [],
    }


def test_fingerprint_is_sha256_of_the_compact_sorted_canonical_json() -> None:
    acl = restricted([[user("a"), user("b")]])

    expected = hashlib.sha256(
        json.dumps(canonical_acl(acl), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert acl_fingerprint(acl) == expected
    assert len(expected) == 64


def test_fingerprint_is_pinned_because_class_ids_are_stored() -> None:
    # Class ids are persisted keys: if this literal changes, every stored class is re-keyed.
    acl = restricted([[user("a"), user("b")], [group("g")]])

    assert canonical_acl(acl) == {
        "realm": "atlassian:acme.atlassian.net",
        "scope": "jira-project:CPM",
        "status": "restricted",
        "levels": [
            ["atlassian:acme.atlassian.net|group:g"],
            ["atlassian:acme.atlassian.net|user:a", "atlassian:acme.atlassian.net|user:b"],
        ],
    }
    assert acl_fingerprint(acl) == (
        "332017db433e4e87f15fac18626121bea4dd6d30f76bc6802f335d22c9fee7ee"
    )


def test_fingerprint_ignores_order_duplicates_reason_and_names() -> None:
    plain = restricted([[user("a"), user("b")], [group("g")]])
    shuffled = restricted(
        [
            [group("g"), group("g")],
            [user("b", display_name="Bea"), user("a", display_name="Al"), user("b")],
            [group("g", display_name="Staff")],
        ],
        reason="a different reason",
    )

    assert canonical_acl(shuffled) == canonical_acl(plain)
    assert acl_fingerprint(shuffled) == acl_fingerprint(plain)


def test_fingerprint_changes_with_scope_or_realm() -> None:
    base = restricted([[user("a")]])
    other_scope = restricted([[user("a")]], scope="jira-project:OPS")
    other_realm = restricted([[user("a", realm=OTHER_REALM)]], realm=OTHER_REALM)

    assert len({acl_fingerprint(acl) for acl in (base, other_scope, other_realm)}) == 3
    assert len({acl_fingerprint(acl) for acl in (base, base)}) == 1


def test_fingerprint_changes_with_status_membership_kind_and_level_structure() -> None:
    inherited = SourceAcl.inherited(realm=REALM, scope=SCOPE)
    one_user = restricted([[user("a")]])
    two_users = restricted([[user("a"), user("b")]])
    a_group_named_like_a_user = restricted([[group("a")]])
    intersection = restricted([[user("a")], [user("b")]])

    fingerprints = {
        acl_fingerprint(acl)
        for acl in (inherited, one_user, two_users, a_group_named_like_a_user, intersection)
    }

    # "a or b" and "a and b" are different classes even though they name the same people.
    assert len(fingerprints) == 5


def test_class_of_names_the_class_by_the_fingerprint() -> None:
    acl = restricted([[user("a")]])

    acl_class = class_of("tenant-1", acl)

    assert isinstance(acl_class, AclClass)
    assert acl_class.tenant_id == "tenant-1"
    assert acl_class.class_id == ClassId(acl_fingerprint(acl))
    assert acl_class.acl == acl


def test_the_same_acl_in_two_tenants_shares_a_class_id_but_not_a_class() -> None:
    acl = SourceAcl.inherited(realm=REALM, scope=SCOPE)

    first, second = class_of("tenant-1", acl), class_of("tenant-2", acl)

    assert first.class_id == second.class_id
    assert first != second


def test_an_unknown_acl_has_no_class() -> None:
    with pytest.raises(ValueError, match="unknown"):
        class_of("tenant-1", SourceAcl.unknown(reason="permissions_unreadable"))
    with pytest.raises(ValueError, match="unknown"):
        class_of("tenant-1", SourceAcl.unknown(reason="x", realm=REALM, scope=SCOPE))
