"""Map captured source ACL evidence onto document permission snapshots.

This is the seam between connector evidence and HarborRAG authorization. The
caller supplies ``directory``, which maps ``SourcePrincipal.key`` to the HarborRAG
principal ids it stands for, with groups, roles and security levels already
expanded by the operator's identity system. The result is imported through the
existing ``set_permissions`` path (``harborrag topology indexing
permissions-import``). Anything the directory does not cover grants nothing.

Keys are matched exactly as captured: a principal's ``id`` is whatever identifier
the source returned (for example a Confluence Cloud group id, or a Data Center
group name), so key the directory by that same identifier.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .acl import SourceAcl


@dataclass(frozen=True, slots=True)
class PermissionTarget:
    """Where and for how long one document snapshot applies."""

    tenant_id: str
    document_id: str
    resolved_at: datetime
    expires_at: datetime


def permission_snapshot(
    acl: SourceAcl,
    *,
    target: PermissionTarget,
    directory: Mapping[str, Iterable[str]],
) -> ResolvedPermissionSnapshot:
    """Build the document-level snapshot for one item's captured ACL.

    - ``inherited``: known and public. The document adds no restriction, and the
      source-scope snapshot still decides.
    - ``restricted``: known and not public; readers are the intersection, over
      levels, of each level's mapped principals.
    - ``unknown``: ``known=False``, which denies every reader and processing.
    """

    if acl.status == "unknown":
        return _snapshot(target, acl, known=False, public=False, allowed=())
    if acl.status == "inherited":
        return _snapshot(target, acl, known=True, public=True, allowed=())
    allowed: set[str] | None = None
    for level in acl.levels:
        members = {pid for principal in level for pid in directory.get(principal.key, ())}
        allowed = members if allowed is None else allowed & members
    return _snapshot(target, acl, known=True, public=False, allowed=tuple(sorted(allowed or ())))


def _snapshot(
    target: PermissionTarget,
    acl: SourceAcl,
    *,
    known: bool,
    public: bool,
    allowed: tuple[str, ...],
) -> ResolvedPermissionSnapshot:
    revision = hashlib.sha256(
        json.dumps(
            {"acl": acl.to_dict(), "allowed": list(allowed), "known": known, "public": public},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ResolvedPermissionSnapshot(
        tenant_id=target.tenant_id,
        resource_kind="document",
        resource_id=target.document_id,
        revision=revision,
        resolved_at=target.resolved_at,
        expires_at=target.expires_at,
        known=known,
        processing_allowed=known,
        public=public,
        allowed_principal_ids=allowed,
    )
