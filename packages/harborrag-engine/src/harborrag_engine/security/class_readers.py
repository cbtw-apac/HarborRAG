"""Who may read an ACL class, and the permission snapshot that records it.

The readers of a source ACL are computed from the directory of its realm:

    R = (readers of the scope) ∩ (readers of level 1) ∩ ... ∩ (readers of level n)

where the readers of the scope are everyone its ``scope_grants`` expand to, plus
every active user when the scope is public, and the readers of a level are
everyone its principals expand to. An ``inherited`` ACL has no levels, so it is
read by exactly the readers of its scope. Those source accounts are then mapped to
HarborRAG principal ids through the identity links.

Every doubt resolves to fewer readers, never more. A directory that is not the
ACL's (another realm or tenant) or that does not know the scope gives nobody;
a level that resolves to nobody leaves nobody; a principal the directory cannot
resolve adds nobody and is reported in :attr:`ClassReaders.unresolved`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from harborrag_core.security.classes import AclClass
from harborrag_core.security.directory import DirectorySnapshot
from harborrag_core.sources.acl import SourcePrincipal
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_engine.security.resolver import expand_all


@dataclass(frozen=True, slots=True)
class ClassReaders:
    """The readers of one ACL class.

    ``principal_ids`` are the HarborRAG principals who may read it, and ``public``
    says anyone may. ``unresolved`` lists principals the ACL names, or readers the
    source grants, that could not be turned into a HarborRAG principal: a group the
    directory does not know, an issue-relative role, or a user with no identity
    link. ``scope_known`` is false when the directory says nothing about the class's
    scope, in which case there are no readers at all.
    """

    principal_ids: frozenset[str]
    public: bool
    unresolved: frozenset[SourcePrincipal]
    scope_known: bool


def class_readers(
    acl_class: AclClass,
    directory: DirectorySnapshot,
    accounts: Mapping[SourcePrincipal, frozenset[str]],
) -> ClassReaders:
    """Compute the readers of ``acl_class`` from ``directory`` and the account links.

    ``accounts`` maps a source account to the HarborRAG principal ids linked to it
    (see :func:`harborrag_engine.security.resolver.account_index`).

    ``public`` is true only for an ``inherited`` ACL on a public scope; a
    ``restricted`` ACL on a public scope is still limited by its levels.
    """

    acl = acl_class.acl
    if directory.realm != acl.realm or directory.tenant_id != acl_class.tenant_id:
        # Not the directory of this ACL: it resolves none of what the ACL names.
        named = frozenset(principal for level in acl.levels for principal in level)
        return ClassReaders(frozenset(), False, named, False)
    scope_is_public = acl.scope in directory.public_scopes
    if not scope_is_public and acl.scope not in directory.scope_grants:
        return ClassReaders(frozenset(), False, frozenset(), False)

    granted, unresolved = expand_all(directory.scope_grants.get(acl.scope, ()), directory)
    readers = set(granted)
    if scope_is_public:
        readers.update(directory.active_users)
    for level in acl.levels:
        level_users, level_unresolved = expand_all(level, directory)
        readers &= level_users
        unresolved |= level_unresolved

    principal_ids: set[str] = set()
    unlinked: set[SourcePrincipal] = set()
    for account in readers:
        linked = accounts.get(account)
        if linked:
            principal_ids.update(linked)
        else:
            unlinked.add(account)
    return ClassReaders(
        principal_ids=frozenset(principal_ids),
        public=scope_is_public and acl.status == "inherited",
        unresolved=frozenset(unresolved | unlinked),
        scope_known=True,
    )


def class_snapshot(
    acl_class: AclClass,
    readers: ClassReaders,
    *,
    resolved_at: datetime,
    expires_at: datetime,
) -> ResolvedPermissionSnapshot:
    """The permission snapshot of ``acl_class`` for its ``readers``.

    - A public class (``readers.public``) lists no principal ids: ``public`` already
      admits every principal, so the ids add nothing, and a large public scope can
      never overflow the snapshot's limit of 10,000 ids.
    - Any other class lists its readers sorted. More than 10,000 of them raises
      ``ValueError`` (pydantic's ``ValidationError``) and is never truncated: a
      shortened list would silently lock readers out.
    - When ``readers.scope_known`` is false the directory said nothing about the
      scope, so the snapshot is ``known=False`` and ``processing_allowed=False``
      with no readers and ``public=False``, whatever ``readers`` otherwise holds.
      Coverage reports then count the class as unknown instead of healthy.

    ``revision`` hashes the class id, ``public`` and the listed ids (plus a marker
    for an unknown scope), so it stays the same while what the snapshot says does
    and changes the moment that changes.
    """

    scope_known = readers.scope_known
    public = scope_known and readers.public
    allowed = () if public or not scope_known else tuple(sorted(readers.principal_ids))
    payload: list[object] = [acl_class.class_id, public, list(allowed)]
    if not scope_known:
        payload.append("unknown-scope")
    revision = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    return ResolvedPermissionSnapshot(
        tenant_id=acl_class.tenant_id,
        resource_kind="class",
        resource_id=acl_class.class_id,
        revision=revision,
        resolved_at=resolved_at,
        expires_at=expires_at,
        known=scope_known,
        processing_allowed=scope_known,
        public=public,
        allowed_principal_ids=allowed,
    )
