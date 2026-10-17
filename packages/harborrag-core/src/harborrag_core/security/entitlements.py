"""The one rule for which datasources a principal is entitled to, and how entitlements narrow.

:func:`entitled_scopes` turns project memberships and the datasources of each project
into the set a principal may read. :func:`narrower` combines two entitlements into the
one that never grants more than either. Both are pure and generic over strings, so the
same rule serves source scope ids and connection ids, and every store of entitlements
(the in-memory fake, a file) shares one implementation instead of restating it.

``ProjectMember`` is imported for typing only: ``harborrag_core.security.directory``
reaches ``harborrag_core.sources``, which this package's ``__init__`` must not import
at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harborrag_core.security.directory import ProjectMember


def entitled_scopes(
    memberships: Iterable[ProjectMember],
    project_scopes: Mapping[str, frozenset[str]],
    principal_id: str,
) -> frozenset[str]:
    """The datasources ``principal_id`` may read: the union over its memberships.

    A membership entitles the whole of its project's datasources, or, when it is
    narrowed to some datasources, only those that are still the project's own. So a
    narrowed membership can neither reach another project's datasources nor keep one
    its project no longer has. A project absent from ``project_scopes`` has none, and
    memberships of other principals entitle nothing.
    """

    scopes: set[str] = set()
    for member in memberships:
        if member.principal_id != principal_id:
            continue
        project = project_scopes.get(member.project_id, frozenset())
        if member.source_scope_ids is None:
            scopes |= project
        else:
            scopes |= member.source_scope_ids & project
    return frozenset(scopes)


def narrower(left: frozenset[str] | None, right: frozenset[str] | None) -> frozenset[str] | None:
    """The tighter of two entitlements: ``None`` gates nothing, so the other one wins."""

    if left is None:
        return right
    if right is None:
        return left
    return left & right


__all__ = ["entitled_scopes", "narrower"]
