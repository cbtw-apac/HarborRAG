from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.repositories.errors import HarborStorageValidationError
from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorCollectionSpec

from .fakes import (
    Distance,
    ExtendedModels,
    ExtendedRawQdrant,
    FakeQdrantClient,
    FakeRawQdrant,
    make_config,
)


@pytest.mark.asyncio
async def test_delete_collection_removes_only_the_tenant_scoped_physical_collection() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    other_context = StorageOperationContext(tenant_id="tenant-b")
    key = repository._queries.spec_key("docs", context)
    other_key = repository._queries.spec_key("docs", other_context)
    repository._specs[key] = VectorCollectionSpec(name="docs", dimension=3)
    repository._specs[other_key] = VectorCollectionSpec(name="docs", dimension=3)

    await repository.delete_collection("docs", context=context)

    assert raw.delete_collection_calls == [repository._queries.collection_name("docs", context)]
    assert key not in repository._specs
    assert other_key in repository._specs


@pytest.mark.asyncio
async def test_ensure_collection_rejects_non_tenant_scoped_spec() -> None:
    raw = FakeRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, tenant_scoped=False)

    with pytest.raises(HarborStorageValidationError):
        await repository.ensure_collection(spec, context=context)


@pytest.mark.asyncio
async def test_ensure_collection_creates_new_collection_with_metadata_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["source", "kind"])

    await repository.ensure_collection(spec, context=context)

    assert raw.create_collection_calls[0]["collection_name"] == repository._queries.collection_name(
        "docs", context
    )
    assert len(raw.create_payload_index_calls) == 2
    assert repository._specs[repository._queries.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_ensure_collection_accepts_concurrent_creator_and_validates_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)

    class RacingRawQdrant(ExtendedRawQdrant):
        async def create_collection(self, **kwargs: Any) -> None:
            self.create_collection_calls.append(kwargs)
            self.exists = True
            raise RuntimeError("collection already exists")

    raw = RacingRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["source"])

    await repository.ensure_collection(spec, context=context)

    assert len(raw.create_collection_calls) == 1
    assert len(raw.create_payload_index_calls) == 1
    assert repository._specs[repository._queries.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_ensure_collection_uses_boolean_index_for_active_lifecycle_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(
        name="docs",
        dimension=3,
        metadata_indexes=["source", "is_active"],
    )

    await repository.ensure_collection(spec, context=context)

    schemas = {call["field_name"]: call["field_schema"] for call in raw.create_payload_index_calls}
    assert schemas == {
        "source": ExtendedModels.PayloadSchemaType.KEYWORD,
        "is_active": ExtendedModels.PayloadSchemaType.BOOL,
    }


@pytest.mark.asyncio
async def test_ensure_collection_repairs_legacy_active_keyword_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = True
    raw.existing_payload_schema = {
        "is_active": SimpleNamespace(data_type=ExtendedModels.PayloadSchemaType.KEYWORD)
    }
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["is_active"])

    await repository.ensure_collection(spec, context=context)

    assert raw.delete_payload_index_calls == [
        {
            "collection_name": repository._queries.collection_name("docs", context),
            "field_name": "is_active",
            "wait": True,
        }
    ]
    assert (
        raw.create_payload_index_calls[0]["field_schema"] is ExtendedModels.PayloadSchemaType.BOOL
    )


@pytest.mark.asyncio
async def test_ensure_collection_matches_existing_and_only_adds_missing_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    raw = ExtendedRawQdrant()
    raw.exists = True
    raw.existing_dimension = 3
    raw.existing_distance = Distance.COSINE
    raw.existing_payload_schema = {"source": object()}
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3, metadata_indexes=["source", "kind"])

    await repository.ensure_collection(spec, context=context)

    assert raw.create_collection_calls == []
    assert len(raw.create_payload_index_calls) == 1
    assert raw.create_payload_index_calls[0]["field_name"] == "kind"
    assert repository._specs[repository._queries.spec_key("docs", context)] is spec


@pytest.mark.asyncio
async def test_ensure_collection_raises_when_existing_dimension_mismatches() -> None:
    raw = ExtendedRawQdrant()
    raw.exists = True
    raw.existing_dimension = 5
    raw.existing_distance = Distance.COSINE
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    spec = VectorCollectionSpec(name="docs", dimension=3)

    with pytest.raises(HarborStorageValidationError):
        await repository.ensure_collection(spec, context=context)


@pytest.mark.asyncio
async def test_collection_exists_delegates_to_database_client() -> None:
    raw = FakeRawQdrant()
    raw.exists = True
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")

    assert await repository.collection_exists("docs", context=context) is True


@pytest.mark.asyncio
async def test_delete_collection_when_absent_only_clears_cache() -> None:
    raw = FakeRawQdrant()
    raw.exists = False
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    context = StorageOperationContext(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorCollectionSpec(
        name="docs", dimension=3
    )

    await repository.delete_collection("docs", context=context)

    assert raw.delete_collection_calls == []
    assert repository._queries.spec_key("docs", context) not in repository._specs
