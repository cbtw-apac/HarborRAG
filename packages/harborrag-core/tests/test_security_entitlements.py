"""One rule says which datasources a principal is entitled to, and how entitlements narrow."""

from __future__ import annotations

import pytest

from harborrag_core.security.directory import ProjectMember
from harborrag_core.security.entitlements import entitled_scopes, narrower


def _member(
    project_id: str, principal_id: str, scopes: frozenset[str] | None = None
) -> ProjectMember:
    return ProjectMember(
        project_id=project_id,
        principal_id=principal_id,
        role="reader",
        source_scope_ids=scopes,
    )


def test_union_over_memberships() -> None:
    memberships = [_member("p1", "alice"), _member("p2", "alice")]
    project_scopes = {"p1": frozenset({"a", "b"}), "p2": frozenset({"c"})}

    assert entitled_scopes(memberships, project_scopes, "alice") == frozenset({"a", "b", "c"})


def test_narrowing_is_bounded_by_the_project() -> None:
    memberships = [_member("p1", "alice", frozenset({"b", "z"}))]
    project_scopes = {"p1": frozenset({"a", "b"})}

    assert entitled_scopes(memberships, project_scopes, "alice") == frozenset({"b"})


def test_a_project_without_scopes_entitles_nothing() -> None:
    memberships = [_member("p9", "alice")]
    project_scopes = {"p1": frozenset({"a", "b"})}

    assert entitled_scopes(memberships, project_scopes, "alice") == frozenset()


def test_other_principals_entitle_nothing() -> None:
    memberships = [_member("p1", "bob")]
    project_scopes = {"p1": frozenset({"a", "b"})}

    assert entitled_scopes(memberships, project_scopes, "alice") == frozenset()


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (None, None, None),
        (None, frozenset({"a"}), frozenset({"a"})),
        (frozenset({"a"}), None, frozenset({"a"})),
        (frozenset({"a", "b"}), frozenset({"b", "c"}), frozenset({"b"})),
        (frozenset(), None, frozenset()),
    ],
)
def test_narrower(
    left: frozenset[str] | None, right: frozenset[str] | None, expected: frozenset[str] | None
) -> None:
    assert narrower(left, right) == expected
