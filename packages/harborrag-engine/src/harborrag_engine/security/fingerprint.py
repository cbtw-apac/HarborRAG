"""Name an ACL: its canonical form, its fingerprint, and the class it defines.

Two source ACLs that grant the same access must land in the same class, however
a connector happened to order or repeat their principals. There is one canonical
form, :meth:`~harborrag_core.sources.acl.SourceAcl.canonical`: it keeps exactly
what decides access (realm, scope, status and the principal levels) and drops
what does not (the free-text ``reason``). The fingerprint hashes it by principal
key, so display names never count either.

A class id is the fingerprint, and class ids are stored. Changing the canonical
form re-keys every class, so ``test_fingerprint_is_pinned_because_class_ids_are_stored``
pins one value.
"""

from __future__ import annotations

import hashlib
import json

from harborrag_core.security.classes import AclClass, ClassId
from harborrag_core.sources.acl import SourceAcl


def canonical_acl(acl: SourceAcl) -> dict[str, object]:
    """The access-relevant content of ``acl`` in one canonical shape.

    It is ``acl.canonical()`` with each principal written as its key: each level
    is the sorted, de-duplicated list of its principal keys, and the levels
    themselves are de-duplicated and sorted. A reader must match every level, so
    neither the order of levels nor a repeated level changes who may read;
    ``[[a, b]]`` ("a or b") and ``[[a], [b]]`` ("a and b") stay different.
    """

    canonical = acl.canonical()
    return {
        "realm": canonical.realm,
        "scope": canonical.scope,
        "status": canonical.status,
        "levels": [[principal.key for principal in level] for level in canonical.levels],
    }


def acl_fingerprint(acl: SourceAcl) -> str:
    """SHA-256 hex of the canonical JSON of ``acl``."""

    payload = json.dumps(canonical_acl(acl), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def class_of(tenant_id: str, acl: SourceAcl) -> AclClass:
    """The class ``acl`` defines in ``tenant_id``, holding the canonical form of ``acl``.

    Every ACL equivalent to ``acl`` defines the very same class. An ``unknown`` ACL
    cannot be evaluated, so it has no class: ``ValueError``.
    """

    if acl.status == "unknown":
        raise ValueError("an unknown source ACL has no ACL class")
    canonical = acl.canonical()
    return AclClass(
        tenant_id=tenant_id, class_id=ClassId(acl_fingerprint(canonical)), acl=canonical
    )
