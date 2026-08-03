from datetime import UTC
from math import inf

import pytest
from chunking_test_fixtures import make_chunk
from pydantic import ValidationError

from harborrag_core.chunking import ChunkRecord
from harborrag_core.schemas.cache import CacheEntry
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorIndexRecord, VectorSearchQuery
from harborrag_core.security import AccessContext


def test_chunk_records_use_the_canonical_identity_shape() -> None:
    record = make_chunk()

    assert record.logical_chunk_id == "logical-chunk:stable"
    assert record.chunk_id == "chunk:exact"
    assert record.document_version_id == "document-version-7"
    assert record.ordinal == 2
    assert record.record_kind.value == "evidence"
    assert record.chunk_kind.value == "text"


def test_chunk_record_rejects_the_removed_storage_id_alias() -> None:
    values = make_chunk().model_dump()
    values["artifact_revision_id"] = "artifact-revision-1"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChunkRecord.model_validate(values)


def test_storage_schemas_are_strict_frozen_and_use_utc_timestamps() -> None:
    entry = CacheEntry(key="key", value="value")

    assert entry.created_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        CacheEntry(key="key", value="value", unknown=True)
    with pytest.raises(ValidationError):
        entry.key = "other"


def test_storage_identifiers_are_normalized_by_pydantic() -> None:
    context = StorageOperationContext.system(tenant_id=" tenant-a ")

    assert context.tenant_id == "tenant-a"
    assert type(context.tenant_id).__name__ == "TenantId"


def test_storage_context_preserves_the_authenticated_principal() -> None:
    access = AccessContext(principal_id="user-1", tenant_id="tenant-a")

    context = StorageOperationContext.for_access(access, operation_kind="retrieve")

    assert context.access is access
    assert context.access.principal_id == "user-1"
    assert context.operation_kind == "retrieve"


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (VectorIndexRecord, {"id": "point", "tenant_id": "tenant", "vector": []}),
        (VectorSearchQuery, {"index_name": "docs", "vector": [inf]}),
    ],
)
def test_dense_vectors_must_be_non_empty_and_finite(
    model: type[VectorIndexRecord] | type[VectorSearchQuery],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)
