"""SQL authorization predicates run before selection limits and graph expansion."""

from typing import Any

from sqlalchemy import and_, exists, false, or_, select

from harborrag_core.base import utc_now
from harborrag_core.security.context import AccessContext

from ..schema import DOCUMENTS
from .policy_schema import (
    BUILD_DOCUMENTS,
    BUILD_PERMISSIONS,
    PERMISSION_GRANTS,
    PERMISSION_SNAPSHOTS,
)


def readable_snapshot(snapshot: Any, access: AccessContext | None) -> Any:
    if access is None:
        return false()
    grant = PERMISSION_GRANTS.alias()
    matching = and_(
        grant.c.tenant_id == snapshot.c.tenant_id,
        grant.c.resource_kind == snapshot.c.resource_kind,
        grant.c.resource_id == snapshot.c.resource_id,
        grant.c.principal_id == access.principal_id,
    )
    return and_(
        snapshot.c.tenant_id == str(access.tenant_id),
        snapshot.c.known.is_(True),
        snapshot.c.resolved_at <= utc_now(),
        snapshot.c.expires_at > utc_now(),
        or_(
            snapshot.c.public.is_(True),
            exists(select(grant.c.principal_id).where(matching, grant.c.allowed.is_(True))),
        ),
        ~exists(select(grant.c.principal_id).where(matching, grant.c.denied.is_(True))),
    )


def authorized_documents(tenant_id: str, access: AccessContext | None) -> Any:
    source = PERMISSION_SNAPSHOTS.alias("source_acl")
    document = PERMISSION_SNAPSHOTS.alias("document_acl")
    return (
        select(DOCUMENTS.c.document_id, DOCUMENTS.c.active_document_version_id)
        .join(
            source,
            and_(
                source.c.tenant_id == DOCUMENTS.c.tenant_id,
                source.c.resource_kind == "source",
                source.c.resource_id == DOCUMENTS.c.source_scope_id,
            ),
        )
        .join(
            document,
            and_(
                document.c.tenant_id == DOCUMENTS.c.tenant_id,
                document.c.resource_kind == "document",
                document.c.resource_id == DOCUMENTS.c.document_id,
            ),
        )
        .where(
            DOCUMENTS.c.tenant_id == tenant_id,
            DOCUMENTS.c.active_document_version_id.is_not(None),
            readable_snapshot(source, access),
            readable_snapshot(document, access),
        )
    )


def build_permissions_current(
    build_id: Any, tenant_id: str, access: AccessContext | None = None, *, serving: bool = False
) -> Any:
    dep = BUILD_PERMISSIONS.alias()
    snapshot = PERMISSION_SNAPSHOTS.alias()
    current = and_(
        snapshot.c.tenant_id == dep.c.tenant_id,
        snapshot.c.resource_kind == dep.c.resource_kind,
        snapshot.c.resource_id == dep.c.resource_id,
        snapshot.c.revision == dep.c.revision,
        snapshot.c.known.is_(True),
        snapshot.c.processing_allowed.is_(True),
        snapshot.c.resolved_at <= utc_now(),
        snapshot.c.expires_at > utc_now(),
    )
    if serving:
        current = and_(current, readable_snapshot(snapshot, access))
    dependencies = and_(dep.c.tenant_id == tenant_id, dep.c.build_id == build_id)
    missing = select(dep.c.resource_id).where(
        dependencies, ~exists(select(snapshot.c.resource_id).where(current))
    )
    inputs = BUILD_DOCUMENTS.alias()
    doc = DOCUMENTS.alias()
    input_predicate = and_(inputs.c.tenant_id == tenant_id, inputs.c.build_id == build_id)
    stale_inputs = exists(
        select(inputs.c.document_id).where(
            input_predicate,
            ~exists(
                select(doc.c.document_id).where(
                    doc.c.tenant_id == inputs.c.tenant_id,
                    doc.c.document_id == inputs.c.document_id,
                    doc.c.active_document_version_id == inputs.c.document_version_id,
                )
            ),
        )
    )
    return and_(
        ~stale_inputs,
        exists(select(inputs.c.document_id).where(input_predicate)),
        exists(select(dep.c.resource_id).where(dependencies, dep.c.resource_kind == "source")),
        exists(select(dep.c.resource_id).where(dependencies, dep.c.resource_kind == "document")),
        ~exists(missing),
    )
