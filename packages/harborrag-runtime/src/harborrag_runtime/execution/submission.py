"""Translate public SDK requests into durable ingestion contracts."""

from __future__ import annotations

import json

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import IngestionRequest
from harborrag_runtime.execution.gateway import prepare_configured_source_submission
from harborrag_runtime.ingestion.connections import ConnectionResolver, ResolvedConnection
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission
from harborrag_runtime.source_query import SourceQuery


def request_source_key(request: IngestionRequest) -> str:
    """Return the ``source_key`` an SDK request names."""
    return request.connection_id or request.connector_name


async def resolve_request_connection(
    resolver: ConnectionResolver, request: IngestionRequest
) -> ResolvedConnection:
    """Load and snapshot the connection an SDK request names."""
    return await resolver.resolve(
        tenant_id=str(request.access.tenant_id), source_key=request_source_key(request)
    )


def build_ingestion_input(
    settings: RuntimeSettings,
    request: IngestionRequest,
    *,
    connection: ResolvedConnection,
) -> PreparedSourceSubmission:
    """Build the single source input shared by direct and Temporal strategies."""

    return prepare_configured_source_submission(
        settings,
        SourceSubmission(
            task_id=request.task_id,
            tenant_id=str(request.access.tenant_id),
            connector_name=request.connector_name,
            connection_id=request.connection_id,
            source_scope_id=request.source_scope_id,
            query=SourceQuery(
                path=request.path,
                pattern=request.pattern,
                recursive=request.recursive,
                updated_after=request.updated_after,
                limit=request.limit,
                include_attachments=request.include_attachments,
                filters_json=json.dumps(
                    request.filters,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
            force_reprocess=request.force_reprocess,
            discovery_page_size=request.discovery_page_size,
            discovery_concurrency=request.discovery_concurrency,
            document_concurrency=request.document_concurrency,
        ),
        connection=connection,
    )
