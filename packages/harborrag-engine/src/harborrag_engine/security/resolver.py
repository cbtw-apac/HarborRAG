"""Turn source principals into the active users they stand for.

A source ACL names users, groups and roles of the source's own identity realm.
:func:`expand` flattens one of them against a synced directory, and
:func:`account_index` says which HarborRAG principals own a source account.

Anything the directory cannot resolve matches nobody and is reported, never
guessed at: an unknown group, a principal of another realm, an issue-relative
role such as Jira's reporter or assignee. Group nesting is walked with an
explicit stack and a seen set, so a membership cycle ends and a very deep chain
cannot hit the recursion limit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from harborrag_core.security.directory import DirectorySnapshot, IdentityLink
from harborrag_core.sources.acl import SourcePrincipal

USER_KIND = "user"
EVERYONE_KIND = "everyone"
ISSUE_ROLE_KIND = "jira_issue_role"
"""Relative to one issue (its reporter, its assignee); a directory cannot resolve it."""


def account_index(links: Iterable[IdentityLink]) -> Mapping[SourcePrincipal, frozenset[str]]:
    """Map each source account to the HarborRAG principal ids linked to it."""

    owners: dict[SourcePrincipal, set[str]] = {}
    for link in links:
        owners.setdefault(link.account, set()).add(link.principal_id)
    return MappingProxyType({account: frozenset(ids) for account, ids in owners.items()})


def expand_all(
    principals: Iterable[SourcePrincipal], directory: DirectorySnapshot
) -> tuple[frozenset[SourcePrincipal], frozenset[SourcePrincipal]]:
    """The active users every one of ``principals`` stands for, and what could not be resolved.

    Equivalent to the union of :func:`expand` over ``principals``, but a group
    shared by several of them is walked once.
    """

    users: set[SourcePrincipal] = set()
    unresolved: set[SourcePrincipal] = set()
    seen: set[SourcePrincipal] = set()
    pending = list(principals)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current.realm != directory.realm or current.kind == ISSUE_ROLE_KIND:
            unresolved.add(current)
        elif current.kind == USER_KIND:
            if current in directory.active_users:
                users.add(current)
        elif current.kind == EVERYONE_KIND:
            users.update(directory.active_users)
        elif current in directory.members:
            pending.extend(directory.members[current])
        else:
            unresolved.add(current)
    return frozenset(users), frozenset(unresolved)


def expand(
    principal: SourcePrincipal, directory: DirectorySnapshot
) -> tuple[frozenset[SourcePrincipal], frozenset[SourcePrincipal]]:
    """The active users ``principal`` stands for, and the principals that could not be resolved.

    - a ``user`` is itself if the directory lists it as active, and nobody otherwise;
    - ``everyone`` is every active user;
    - a principal with an entry in ``directory.members`` is its members, expanded in turn;
    - anything else is unresolved: a principal of another realm, an issue-relative
      role, or a group the directory has never heard of.
    """

    return expand_all((principal,), directory)
