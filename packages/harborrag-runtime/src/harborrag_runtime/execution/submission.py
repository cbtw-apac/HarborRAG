"""Translate public SDK requests into durable ingestion contracts."""

from __future__ import annotations

import json

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import IngestionRequest
from harborrag_runtime.temporal.schemas import SourceIngestionInput
from harborrag_runtime.temporal.source_query import SourceQuery
from harborrag_runtime.temporal.submission import SourceSubmission, build_source_input


def build_ingestion_input(
    settings: RuntimeSettings,
    request: IngestionRequest,
) -> SourceIngestionInput:
    """Build the single source input shared by direct and Temporal strategies."""

    return build_source_input(
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
    )
