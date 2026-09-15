"""Select missing desired generations without starving later documents."""

from sqlalchemy import and_, exists, func, select

from harborrag_core.base import utc_now

from ..schema import DOCUMENTS
from .policy_schema import INDEXING_CONFIGS, PERMISSION_SNAPSHOTS
from .schema import TOPOLOGY_JOBS, TOPOLOGY_POLICIES


def missing_generations(tenant_id: str, limit: int):  # type: ignore[no-untyped-def]
    source = PERMISSION_SNAPSHOTS.alias("source_permissions")
    document = PERMISSION_SNAPSHOTS.alias("document_permissions")
    return (
        select(DOCUMENTS)
        .join(
            TOPOLOGY_POLICIES,
            and_(
                TOPOLOGY_POLICIES.c.tenant_id == DOCUMENTS.c.tenant_id,
                TOPOLOGY_POLICIES.c.source_scope_id == DOCUMENTS.c.source_scope_id,
                TOPOLOGY_POLICIES.c.enabled.is_(True),
            ),
        )
        .join(
            INDEXING_CONFIGS,
            and_(
                INDEXING_CONFIGS.c.tenant_id == DOCUMENTS.c.tenant_id,
                INDEXING_CONFIGS.c.enabled.is_(True),
                INDEXING_CONFIGS.c.prohibited.is_(False),
            ),
        )
        .outerjoin(
            source,
            and_(
                source.c.tenant_id == DOCUMENTS.c.tenant_id,
                source.c.resource_kind == "source",
                source.c.resource_id == DOCUMENTS.c.source_scope_id,
            ),
        )
        .outerjoin(
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
            ~exists(
                select(TOPOLOGY_JOBS.c.job_id).where(
                    TOPOLOGY_JOBS.c.tenant_id == DOCUMENTS.c.tenant_id,
                    TOPOLOGY_JOBS.c.document_version_id == DOCUMENTS.c.active_document_version_id,
                    TOPOLOGY_JOBS.c.policy_revision == TOPOLOGY_POLICIES.c.revision,
                    TOPOLOGY_JOBS.c.config_epoch == INDEXING_CONFIGS.c.epoch,
                    TOPOLOGY_JOBS.c.source_permission_revision
                    == func.coalesce(source.c.revision, ""),
                    TOPOLOGY_JOBS.c.document_permission_revision
                    == func.coalesce(document.c.revision, ""),
                    ~and_(
                        func.coalesce(TOPOLOGY_JOBS.c.error_code, "")
                        == "permissions_unknown_or_processing_denied",
                        source.c.known.is_(True),
                        source.c.processing_allowed.is_(True),
                        source.c.resolved_at <= utc_now(),
                        source.c.expires_at > utc_now(),
                        document.c.known.is_(True),
                        document.c.processing_allowed.is_(True),
                        document.c.resolved_at <= utc_now(),
                        document.c.expires_at > utc_now(),
                    ),
                )
            ),
        )
        .order_by(DOCUMENTS.c.document_id)
        .limit(max(1, min(limit, 10000)))
        .with_for_update(of=DOCUMENTS)
    )
