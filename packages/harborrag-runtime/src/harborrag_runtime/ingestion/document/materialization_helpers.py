"""Pure helpers for source and representation materialization."""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import RepresentationSet

from .models import DocumentReleaseRequest


def enrich_raw_document(
    raw: RawDocument,
    request: DocumentReleaseRequest,
) -> RawDocument:
    """Add stable source identity without introducing runtime metadata."""

    source = request.source_identity
    metadata = {
        **raw.metadata,
        "source_version": request.admission.source_version,
        "tenant_id": source.tenant_id,
        "connector_type": source.connector_type.value,
        "connection_id": source.connection_id,
        "source_scope_id": source.source_scope_id,
        "source_item_id": source.source_item_id,
        "binding_kind": source.binding.kind.value,
    }
    if source.binding.parent_source_item_id is not None:
        metadata["parent_source_item_id"] = source.binding.parent_source_item_id
    return replace(raw, metadata=metadata)


def representation_artifact_name(
    representations: RepresentationSet,
) -> str:
    """Address the combined dense/sparse representation profile."""

    return encoded_identifier(
        "encoder-profile",
        {
            "dense": representations.dense_profile_id,
            "sparse": representations.sparse_profile_id,
        },
    )
