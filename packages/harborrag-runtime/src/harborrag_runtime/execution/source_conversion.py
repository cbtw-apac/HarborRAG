"""Convert neutral submission contracts into source ingestion requests."""

from __future__ import annotations

import json
from datetime import datetime

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import ProcessingProfile
from harborrag_runtime.ingestion.source.models import SourceIngestionRequest
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission
from harborrag_runtime.source_query import ProcessingProfileInput


def to_source_request(source: PreparedSourceSubmission) -> SourceIngestionRequest:
    query = source.query
    updated_after = (
        datetime.fromisoformat(query.updated_after) if query.updated_after is not None else None
    )
    return SourceIngestionRequest(
        tenant_id=source.tenant_id,
        task_id=source.task_id,
        connector_name=source.connector_name,
        connector_type=ConnectorType(source.connector_type),
        connection_id=source.connection_id,
        source_scope_id=source.source_scope_id,
        configuration_fingerprint=source.configuration_fingerprint,
        processing=to_processing_profile(source.processing),
        query=ConnectorQuery(
            path=query.path,
            pattern=query.pattern,
            recursive=query.recursive,
            updated_after=updated_after,
            limit=query.limit,
            include_attachments=query.include_attachments,
            filters=json.loads(query.filters_json),
        ),
        force_reprocess=source.force_reprocess,
        discovery_page_size=source.discovery_page_size,
        discovery_concurrency=source.discovery_concurrency,
        document_concurrency=source.document_concurrency,
        missing_threshold=source.missing_threshold,
    )


def to_processing_profile(
    processing: ProcessingProfileInput,
) -> ProcessingProfile:
    return ProcessingProfile(
        parser_profile=processing.parser_profile,
        normalizer_version=processing.normalizer_version,
        chunk_strategy=processing.chunk_strategy,
        dense_encoder_profile=processing.dense_encoder_profile,
        sparse_encoder_profile=processing.sparse_encoder_profile,
        graph_projection_version=processing.graph_projection_version,
        vector_projection_schema=processing.vector_projection_schema,
    )
