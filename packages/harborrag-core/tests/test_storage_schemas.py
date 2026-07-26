from datetime import UTC
from math import inf

import pytest
from pydantic import ValidationError

from harborrag_core.schemas.cache import CacheEntry
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorPoint, VectorSearchQuery


def test_chunk_records_use_the_canonical_identity_shape() -> None:
    record = ChunkRecord(
        logical_chunk_id="logical-1",
        chunk_revision_id="revision-1",
        tenant_id="tenant-1",
        document_id="document-1",
        document_version_id="document-version-1",
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        ordinal=2,
        role="content",
        content="content",
        content_hash="hash",
    )

    assert record.logical_chunk_id == "logical-1"
    assert record.chunk_revision_id == "revision-1"
    assert record.artifact_id == "artifact-1"
    assert record.artifact_revision_id == "artifact-revision-1"
    assert record.ordinal == 2
    assert record.role == "content"


def test_chunk_record_rejects_the_removed_storage_id_alias() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChunkRecord(
            id="storage-id",
            logical_chunk_id="logical-id",
            chunk_revision_id="revision-1",
            tenant_id="tenant-1",
            document_id="document-1",
            document_version_id="document-version-1",
            artifact_id="artifact-1",
            artifact_revision_id="artifact-revision-1",
            ordinal=0,
            role="content",
            content="content",
            content_hash="hash",
        )


def test_storage_schemas_are_strict_frozen_and_use_utc_timestamps() -> None:
    entry = CacheEntry(key="key", value="value")

    assert entry.created_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        CacheEntry(key="key", value="value", unknown=True)
    with pytest.raises(ValidationError):
        entry.key = "other"


def test_storage_identifiers_are_normalized_by_pydantic() -> None:
    context = StorageOperationContext(tenant_id=" tenant-a ")

    assert context.tenant_id == "tenant-a"
    assert type(context.tenant_id).__name__ == "TenantId"


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (VectorPoint, {"id": "point", "tenant_id": "tenant", "vector": []}),
        (VectorSearchQuery, {"collection": "docs", "vector": [inf]}),
    ],
)
def test_dense_vectors_must_be_non_empty_and_finite(
    model: type[VectorPoint] | type[VectorSearchQuery],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)
