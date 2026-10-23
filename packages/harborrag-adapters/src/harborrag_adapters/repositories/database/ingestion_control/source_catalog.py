"""Permission-scoped source discovery for reader workflows."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.engine import RowMapping

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.ingestion import ReadableSource, SourceCatalogQuery, SourceScanState
from harborrag_core.ingestion.source_catalog import EntitySummaryState, SourceEntityFacet
from harborrag_core.summaries import SummaryFacet

from .schema import DOCUMENT_VERSIONS, DOCUMENTS, SOURCE_SCANS, SOURCE_SCOPES
from .summary_schema import SUMMARY_SCOPES
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
                DOCUMENTS.c.tenant_id,
                DOCUMENTS.c.source_scope_id,
                func.min(DOCUMENTS.c.connector_type).label("connector_type"),
                func.count(DOCUMENTS.c.document_id).label("active_document_count"),
                func.max(DOCUMENT_VERSIONS.c.activated_at).label("last_successful_ingestion_at"),
            )
            .join(
                DOCUMENT_VERSIONS,
                DOCUMENT_VERSIONS.c.document_version_id == DOCUMENTS.c.active_document_version_id,
            )
            .where(DOCUMENTS.c.active_document_version_id.is_not(None))
            .group_by(DOCUMENTS.c.tenant_id, DOCUMENTS.c.source_scope_id)
            .subquery("source_publication")
        )
        permission = PERMISSION_SNAPSHOTS.alias("source_catalog_acl")
        # The scope's applied summary policy is what declares its entity facets --
        # the same row the worker wrote the entity points under -- so the facets a
        # caller is shown are the ones ``find_entities`` can actually filter on.
        summary = SUMMARY_SCOPES.alias("source_catalog_summary")
        shared = (
            request.access is not None
            and str(request.access.tenant_id) == request.tenant_id
            and request.access.corpus_mode == "tenant_shared"
        )
        if shared:
            statement = (
                select(
                    publication.c.source_scope_id,
                    publication.c.connector_type,
                    latest.c.status,
                    latest.c.started_at,
                    latest.c.completed_at,
                    successful.c.last_successful_source_check_at,
                    publication.c.last_successful_ingestion_at,
                    publication.c.active_document_count,
                    summary.c.policy.label("summary_policy"),
                    summary.c.execution.label("summary_execution"),
                )
                .outerjoin(
                    latest_sequence,
                    latest_sequence.c.source_scope_id == publication.c.source_scope_id,
                )
                .outerjoin(
                    summary,
                    and_(
                        summary.c.tenant_id == publication.c.tenant_id,
                        summary.c.source_scope_id == publication.c.source_scope_id,
                    ),
                )
                .outerjoin(
                    latest,
                    and_(
                        latest.c.source_scope_id == latest_sequence.c.source_scope_id,
                        latest.c.scan_sequence == latest_sequence.c.scan_sequence,
                    ),
                )
                .outerjoin(
                    successful, successful.c.source_scope_id == publication.c.source_scope_id
                )
                .where(publication.c.tenant_id == request.tenant_id)
            )
            if request.source_scope_ids:
                statement = statement.where(
                    publication.c.source_scope_id.in_(request.source_scope_ids)
                )
            if request.connector_types:
                statement = statement.where(
                    publication.c.connector_type.in_(request.connector_types)
                )
            if request.after_source_scope_id is not None:
                statement = statement.where(
                    publication.c.source_scope_id > request.after_source_scope_id
                )
            async with self._client.sessions() as session:
                rows = (
                    (
                        await session.execute(
                            statement.order_by(publication.c.source_scope_id).limit(bound)
                        )
                    )
                    .mappings()
                    .all()
                )
            return tuple(_source_from_row(row) for row in rows)
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
                summary.c.policy.label("summary_policy"),
                summary.c.execution.label("summary_execution"),
            )
            .outerjoin(
                latest_sequence,
                latest_sequence.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id,
            )
            .outerjoin(
                summary,
                and_(
                    summary.c.tenant_id == SOURCE_SCOPES.c.tenant_id,
                    summary.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id,
                ),
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
                and_(
                    publication.c.tenant_id == SOURCE_SCOPES.c.tenant_id,
                    publication.c.source_scope_id == SOURCE_SCOPES.c.source_scope_id,
                ),
            )
            .where(
                SOURCE_SCOPES.c.tenant_id == request.tenant_id,
            )
        )
        statement = statement.join(
            permission,
            and_(
                permission.c.tenant_id == SOURCE_SCOPES.c.tenant_id,
                permission.c.resource_kind == "source",
                permission.c.resource_id == SOURCE_SCOPES.c.source_scope_id,
            ),
        ).where(readable_snapshot(permission, request.access))
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
        entity_facets=_entity_facets(values["summary_policy"]),
        entity_summaries=_entity_summaries(values["summary_policy"], values["summary_execution"]),
    )


def _entity_facets(policy: object) -> tuple[SourceEntityFacet, ...]:
    """The facets the applied summary policy declares, or none.

    Read through ``SummaryFacet`` so a policy persisted in an older shape still
    lists its facets; a row that does not validate lists none rather than failing
    source discovery over a summary projection.
    """

    raw = policy.get("facets") if isinstance(policy, Mapping) else None
    if not isinstance(raw, list):
        return ()
    try:
        facets = [SummaryFacet.model_validate(item) for item in raw]
    except ValidationError:
        return ()
    return tuple(
        SourceEntityFacet(name=facet.name, type=facet.kind, field=facet.field)
        for facet in facets[:12]
    )


_SUMMARY_STATES: dict[str, EntitySummaryState] = {
    "idle": "idle",
    "queued": "queued",
    "running": "running",
    "blocked": "blocked",
    "failed": "failed",
}


def _entity_summaries(policy: object, execution: object) -> EntitySummaryState | None:
    """``disabled`` without a policy, else the run state; unknown states are omitted."""

    if policy is None:
        return "disabled"
    return _SUMMARY_STATES.get(execution) if isinstance(execution, str) else None


__all__ = ["SourceCatalogReader"]
