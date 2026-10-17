"""Name an ACL: its canonical form, its fingerprint, and the class it defines.

Two source ACLs that grant the same access must land in the same class, however
a connector happened to order or repeat their principals. The canonical form
keeps exactly what decides access (realm, scope, status and the principal
levels) and drops what does not (the free-text ``reason`` and display names).

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

    Each level becomes the sorted, de-duplicated list of its principal keys, and
    the levels themselves are de-duplicated and sorted. A reader must match every
    level, so neither the order of levels nor a repeated level changes who may
    read; ``[[a, b]]`` ("a or b") and ``[[a], [b]]`` ("a and b") stay different.
    """

    levels = sorted({tuple(sorted({p.key for p in level})) for level in acl.levels})
    return {
        "realm": acl.realm,
        "scope": acl.scope,
        "status": acl.status,
        "levels": [list(level) for level in levels],
    }


def acl_fingerprint(acl: SourceAcl) -> str:
    """SHA-256 hex of the canonical JSON of ``acl``."""

    payload = json.dumps(canonical_acl(acl), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def class_of(tenant_id: str, acl: SourceAcl) -> AclClass:
    """The class ``acl`` defines in ``tenant_id``.

    An ``unknown`` ACL cannot be evaluated, so it has no class: ``ValueError``.
    """

    if acl.status == "unknown":
        raise ValueError("an unknown source ACL has no ACL class")
    return AclClass(tenant_id=tenant_id, class_id=ClassId(acl_fingerprint(acl)), acl=acl)
