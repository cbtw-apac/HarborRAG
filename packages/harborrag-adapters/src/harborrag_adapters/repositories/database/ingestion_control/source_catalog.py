"""Permission-scoped source discovery for reader workflows."""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.engine import RowMapping

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.ingestion import ReadableSource, SourceCatalogQuery, SourceScanState

from .schema import DOCUMENT_VERSIONS, DOCUMENTS, SOURCE_SCANS, SOURCE_SCOPES
from .topology.authorization import readable_snapshot
from .topology.policy_schema import PERMISSION_SNAPSHOTS


class SourceCatalogReader:
    """List only corpus scopes authorized by a current source permission snapshot."""

    _client: SQLAlchemyDBClient

    async def list_readable_sources(
        self,
        request: SourceCatalogQuery,
    ) -> tuple[ReadableSource, ...]:
        bound = request.limit
        latest_sequence = (
            select(
                SOURCE_SCANS.c.source_scope_id,
                func.max(SOURCE_SCANS.c.scan_sequence).label("scan_sequence"),
            )
            .group_by(SOURCE_SCANS.c.source_scope_id)
            .subquery("latest_source_scan")
        )
        latest = SOURCE_SCANS.alias("source_scan")
        successful = (
            select(
                SOURCE_SCANS.c.source_scope_id,
                func.max(SOURCE_SCANS.c.completed_at).label("last_successful_source_check_at"),
            )
            .where(SOURCE_SCANS.c.status == SourceScanState.COMPLETED.value)
            .group_by(SOURCE_SCANS.c.source_scope_id)
            .subquery("successful_source_scan")
        )
        publication = (
            select(
                DOCUMENTS.c.source_scope_id,
                func.count(DOCUMENTS.c.document_id).label("active_document_count"),
                func.max(DOCUMENT_VERSIONS.c.activated_at).label("last_successful_ingestion_at"),
            )
            .join(
                DOCUMENT_VERSIONS,
                DOCUMENT_VERSIONS.c.document_version_id == DOCUMENTS.c.active_document_version_id,
            )
            .where(DOCUMENTS.c.active_document_version_id.is_not(None))
            .group_by(DOCUMENTS.c.source_scope_id)
            .subquery("source_publication")
        )
        permission = PERMISSION_SNAPSHOTS.alias("source_catalog_acl")
        statement = (
            select(
                SOURCE_SCOPES.c.source_scope_id,
                SOURCE_SCOPES.c.connector_type,
                latest.c.status,
                latest.c.started_at,
                latest.c.completed_at,
                successful.c.last_successful_source_check_at,
                publication.c.last_successful_ingestion_at,
                publication.c.active_document_count,
            )
            .join(
                permission,
                and_(
                    permission.c.tenant_id == SOURCE_SCOPES.c.tenant_id,
                    permission.c.resource_kind == "source",
                    permission.c.resource_id == SOURCE_SCOPES.c.source_scope_id,
                ),
            )
            .outerjoin(
                latest_sequence,
                latest_sequence.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id,
            )
            .outerjoin(
                latest,
                and_(
                    latest.c.source_scope_id == latest_sequence.c.source_scope_id,
                    latest.c.scan_sequence == latest_sequence.c.scan_sequence,
                ),
            )
            .outerjoin(successful, successful.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id)
            .outerjoin(
                publication,
                publication.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id,
            )
            .where(
                SOURCE_SCOPES.c.tenant_id == request.tenant_id,
                readable_snapshot(permission, request.access),
            )
        )
        if request.source_scope_ids:
            statement = statement.where(
                SOURCE_SCOPES.c.source_scope_id.in_(request.source_scope_ids)
            )
        if request.connector_types:
            statement = statement.where(SOURCE_SCOPES.c.connector_type.in_(request.connector_types))
        if request.after_source_scope_id is not None:
            statement = statement.where(
                SOURCE_SCOPES.c.source_scope_id > request.after_source_scope_id
            )
        async with self._client.sessions() as session:
            rows = (
                (
                    await session.execute(
                        statement.order_by(SOURCE_SCOPES.c.source_scope_id).limit(bound)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_source_from_row(row) for row in rows)


def _source_from_row(values: RowMapping) -> ReadableSource:
    source_scope_id = str(values["source_scope_id"])
    connector_type = str(values["connector_type"])
    completed_at = values["completed_at"]
    return ReadableSource(
        source_scope_id=source_scope_id,
        connector_type=connector_type,
        display_name=f"{connector_type} · {source_scope_id}",
        ingestion_state=values["status"],
        last_source_check_at=completed_at or values["started_at"],
        last_successful_source_check_at=values["last_successful_source_check_at"],
        last_successful_ingestion_at=values["last_successful_ingestion_at"],
        active_document_count=int(values["active_document_count"] or 0),
    )


__all__ = ["SourceCatalogReader"]
