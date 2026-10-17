"""Per-resource access decisions: may this context read these resources?

A resource is read through the document it derives from. The document's ACL class,
its datasource and the permission snapshot of that class decide, in this order
(the first rule that applies answers):

1. The document is ``ref.id`` for a document and ``ref.document_id`` otherwise.
   Without one, or one that is not in ``documents``: ``no_class``.
2. The quarantine class: ``quarantined``. Nobody is ever a member.
3. A class that is not in ``classes``, or is filed there under a key that is not its
   own ``class_id``: ``no_class``; one of another tenant than the context's:
   ``tenant_mismatch``.
4. A ``serve`` context entitled to a set of datasources that lacks this one:
   ``not_entitled``. An empty set entitles nothing; only ``None`` (a tenant with no
   RBAC) skips the gate. It applies in both corpus modes.
5. A ``serve`` context in a ``tenant_shared`` corpus: ``tenant_shared`` (allowed).
6. A class without a snapshot of its own (a ``class`` snapshot whose ``resource_id``
   is the class id): ``unknown_snapshot``.
7. A ``process`` context reads by processing permission: ``processing_allowed``
   when the snapshot is known, current and processing-allowed, else
   ``processing_disallowed``.
8. A class that is not public, in a realm whose directory is stale: ``stale_realm``.
9. The snapshot's own answer: ``public`` or ``member`` when it lets the principal
   read, else ``not_member``.

Everything not allowed above is denied, and a doubt is never resolved towards
access. Nothing here raises for well-formed input: a denial is a :class:`Decision`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from harborrag_core.security.classes import QUARANTINE_CLASS, AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import Decision, DocumentAccess
from harborrag_core.security.resources import ResourceRef
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot


def decide(
    access: AccessContext,
    refs: Sequence[ResourceRef],
    *,
    documents: Mapping[str, DocumentAccess],
    classes: Mapping[ClassId, AclClass],
    snapshots: Mapping[ClassId, ResolvedPermissionSnapshot],
    stale_realms: frozenset[str],
    now: datetime,
) -> list[Decision]:
    """One :class:`Decision` per ref of ``refs``, in the same order.

    ``documents`` maps a document id to its class and datasource, ``classes`` and
    ``snapshots`` are keyed by class id, and ``stale_realms`` names the realms whose
    directory is out of date. ``now`` must be timezone-aware: a naive clock cannot be
    compared with a snapshot's window, so it raises ``ValueError`` instead of
    guessing.
    """

    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return [
        _decide_one(
            access,
            ref,
            documents=documents,
            classes=classes,
            snapshots=snapshots,
            stale_realms=stale_realms,
            now=now,
        )
        for ref in refs
    ]


def _decide_one(
    access: AccessContext,
    ref: ResourceRef,
    *,
    documents: Mapping[str, DocumentAccess],
    classes: Mapping[ClassId, AclClass],
    snapshots: Mapping[ClassId, ResolvedPermissionSnapshot],
    stale_realms: frozenset[str],
    now: datetime,
) -> Decision:
    document_id = ref.id if ref.kind == "document" else ref.document_id
    document = None if document_id is None else documents.get(document_id)
    if document is None:
        return Decision(False, "no_class")

    class_id = document.class_id
    if class_id == QUARANTINE_CLASS:
        return Decision(False, "quarantined", class_id)

    acl_class = classes.get(class_id)
    if acl_class is None or acl_class.class_id != class_id:
        # A class filed under another class's key is not trusted: it would lend its
        # realm and tenant to a class it is not.
        return Decision(False, "no_class", class_id)
    if acl_class.tenant_id != str(access.tenant_id):
        return Decision(False, "tenant_mismatch", class_id)

    # Anything that is not a background context is held to the serve rules.
    serving = access.purpose != "process"
    entitled = access.entitled_source_scopes
    if serving and entitled is not None and document.source_scope_id not in entitled:
        return Decision(False, "not_entitled", class_id)
    if serving and access.corpus_mode == "tenant_shared":
        return Decision(True, "tenant_shared", class_id)

    snapshot = snapshots.get(class_id)
    if snapshot is None or snapshot.resource_kind != "class" or snapshot.resource_id != class_id:
        # A snapshot that is not this class's own (another class's, or a source's or
        # document's) is no evidence about it, so it counts as missing.
        return Decision(False, "unknown_snapshot", class_id)
    if not serving:
        if _may_process(snapshot, access, now):
            return Decision(True, "processing_allowed", class_id)
        return Decision(False, "processing_disallowed", class_id)

    if not snapshot.public and acl_class.acl.realm in stale_realms:
        return Decision(False, "stale_realm", class_id)
    if snapshot.can_read(access, now=now):
        return Decision(True, "public" if snapshot.public else "member", class_id)
    return Decision(False, "not_member", class_id)


def _may_process(
    snapshot: ResolvedPermissionSnapshot, access: AccessContext, now: datetime
) -> bool:
    """Whether ``snapshot`` lets a background context read its class.

    The snapshot must be known, processing-allowed and inside its validity window
    (``resolved_at <= now < expires_at``). It must also be the context's tenant's:
    ``can_read`` applies that check to a serve context, and a process context never
    goes through ``can_read``, so a snapshot of another tenant is refused here.
    """

    return (
        snapshot.tenant_id == str(access.tenant_id)
        and snapshot.known
        and snapshot.processing_allowed
        and snapshot.resolved_at <= now < snapshot.expires_at
    )
