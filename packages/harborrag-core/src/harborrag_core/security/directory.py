"""The identity directory behind source ACLs, and the records access keeps about it.

A source ACL names principals in the source's own identity namespace (its
*realm*). To decide whether a HarborRAG user is one of them, the directory of
that realm is synced: who belongs to each group, role or level, who may read each
scope, and which users are active. :class:`IdentityLink` ties a HarborRAG
principal to its account in a realm.

Everything here is immutable and validated on construction; a malformed value
raises :class:`ValueError` instead of being stored, so no later gate has to
guess what it meant.

This module is deliberately not re-exported from ``harborrag_core.security``:
it needs :class:`~harborrag_core.sources.acl.SourcePrincipal`, and importing
``harborrag_core.sources`` from the package ``__init__`` would loop back into
``harborrag_core.domain``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import get_args

from harborrag_core.domain.member import Role
from harborrag_core.security._validation import require_text, text_items
from harborrag_core.security.decisions import Decision
from harborrag_core.security.resources import ResourceRef
from harborrag_core.sources.acl import SourcePrincipal

_ROLES: frozenset[str] = frozenset(get_args(Role))


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_principal(value: object, realm: str, name: str) -> None:
    if not isinstance(value, SourcePrincipal):
        raise ValueError(f"{name} must be a source principal")
    if value.realm != realm:
        raise ValueError(f"{name} {value.key!r} is outside the directory realm {realm!r}")


def _principals(values: object, realm: str, name: str) -> frozenset[SourcePrincipal]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be a collection of source principals")
    principals = frozenset(values)
    for principal in principals:
        _require_principal(principal, realm, name)
    return principals


MAX_PRINCIPAL_ID_LENGTH = 255
"""Longest HarborRAG principal id: the bound of ``AccessContext.principal_id``."""


@dataclass(frozen=True, slots=True)
class IdentityLink:
    """A HarborRAG principal and its user account in one source realm.

    ``principal_id`` is at most :data:`MAX_PRINCIPAL_ID_LENGTH` characters, so every
    linked principal can appear in a class snapshot and in an access context.
    """

    principal_id: str
    account: SourcePrincipal

    def __post_init__(self) -> None:
        require_text(self.principal_id, "identity link principal_id")
        if len(self.principal_id) > MAX_PRINCIPAL_ID_LENGTH:
            raise ValueError(
                f"identity link principal_id must be at most {MAX_PRINCIPAL_ID_LENGTH} characters"
            )
        if not isinstance(self.account, SourcePrincipal):
            raise ValueError("identity link account must be a source principal")
        if self.account.kind != "user":
            raise ValueError(
                f"identity link account must be a user account, not {self.account.kind!r}"
            )


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    """One realm's directory as of ``synced_at``.

    ``members`` maps a group, role or level to its direct members (a member may
    itself be a group; resolving nesting is the reader's job). ``scope_grants``
    maps a scope to the principals who may read it. ``active_users`` are the
    users the source still considers active. ``public_scopes`` are readable
    anonymously. Every principal must belong to ``realm``.
    """

    tenant_id: str
    realm: str
    synced_at: datetime
    members: Mapping[SourcePrincipal, frozenset[SourcePrincipal]]
    scope_grants: Mapping[str, frozenset[SourcePrincipal]]
    active_users: frozenset[SourcePrincipal]
    public_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        require_text(self.tenant_id, "directory tenant_id")
        require_text(self.realm, "directory realm")
        _require_aware(self.synced_at, "directory synced_at")
        if not isinstance(self.members, Mapping) or not isinstance(self.scope_grants, Mapping):
            raise ValueError("directory members and scope_grants must be mappings")
        members: dict[SourcePrincipal, frozenset[SourcePrincipal]] = {}
        for group, direct in self.members.items():
            _require_principal(group, self.realm, "directory group")
            members[group] = _principals(direct, self.realm, "directory member")
        grants: dict[str, frozenset[SourcePrincipal]] = {}
        for scope, readers in self.scope_grants.items():
            require_text(scope, "directory scope")
            grants[scope] = _principals(readers, self.realm, "directory scope reader")
        object.__setattr__(self, "members", MappingProxyType(members))
        object.__setattr__(self, "scope_grants", MappingProxyType(grants))
        object.__setattr__(
            self, "active_users", _principals(self.active_users, self.realm, "directory user")
        )
        object.__setattr__(
            self, "public_scopes", frozenset(text_items(self.public_scopes, "directory scope"))
        )


@dataclass(frozen=True, slots=True)
class DecisionLogEntry:
    """One recorded access decision: who asked, on whose behalf, for what, and the answer."""

    tenant_id: str
    principal_id: str
    actor_id: str | None
    resource: ResourceRef
    decision: Decision
    at: datetime

    def __post_init__(self) -> None:
        require_text(self.tenant_id, "decision log entry tenant_id")
        require_text(self.principal_id, "decision log entry principal_id")
        if self.actor_id is not None:
            require_text(self.actor_id, "decision log entry actor_id")
        if not isinstance(self.resource, ResourceRef):
            raise ValueError("decision log entry resource must be a ResourceRef")
        if not isinstance(self.decision, Decision):
            raise ValueError("decision log entry decision must be a Decision")
        _require_aware(self.at, "decision log entry at")


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """What a source changed since a point in time.

    ``item_keys`` are the changed items, ``directory_changed`` says the realm's
    directory must be re-read, and ``cursor`` is where the next
    ``changed_since`` call should resume.
    """

    item_keys: tuple[str, ...]
    directory_changed: bool
    cursor: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_keys", text_items(self.item_keys, "change batch item key"))
        if not isinstance(self.directory_changed, bool):
            raise ValueError("change batch directory_changed must be a bool")
        _require_aware(self.cursor, "change batch cursor")


@dataclass(frozen=True, slots=True)
class ProjectMember:
    """One AUTA RBAC grant: ``principal_id`` holds ``role`` in ``project_id``.

    ``source_scope_ids`` narrows the grant to some of the project's datasources.
    ``None`` means every datasource of the project; an empty set means none.
    """

    project_id: str
    principal_id: str
    role: Role
    source_scope_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        require_text(self.project_id, "project member project_id")
        require_text(self.principal_id, "project member principal_id")
        if not isinstance(self.role, str) or self.role not in _ROLES:
            raise ValueError(f"unsupported project member role: {self.role!r}")
        if self.source_scope_ids is not None:
            object.__setattr__(
                self,
                "source_scope_ids",
                frozenset(text_items(self.source_scope_ids, "project member source scope")),
            )
