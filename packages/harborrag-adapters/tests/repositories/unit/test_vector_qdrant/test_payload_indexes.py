"""Declared source-field indexes are typed, and the indexed set is queryable."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorIndexSpec

from .fakes import ExtendedModels, ExtendedRawQdrant, FakeQdrantClient, make_config


def _repository(
    monkeypatch: pytest.MonkeyPatch, raw: ExtendedRawQdrant
) -> tuple[QdrantVectorRepository, StorageOperationContext]:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    monkeypatch.setattr(query_module, "qm", ExtendedModels)
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    return repository, StorageOperationContext.system(tenant_id="tenant-a")


def test_a_float_index_must_also_be_a_metadata_index() -> None:
    with pytest.raises(ValueError, match="float metadata indexes"):
        VectorIndexSpec(index_name="docs", dimension=3, float_metadata_indexes=["fields.years"])


@pytest.mark.asyncio
async def test_a_numeric_source_field_gets_a_range_capable_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyword index on a number would leave every ``gte`` filter scanning."""

    raw = ExtendedRawQdrant()
    raw.exists = False
    repository, context = _repository(monkeypatch, raw)

    await repository.ensure_index(
        VectorIndexSpec(
            index_name="docs",
            dimension=3,
            metadata_indexes=["status", "fields.skill_set", "fields.years_of_experience"],
            float_metadata_indexes=["fields.years_of_experience"],
        ),
        context=context,
    )

    kinds = {call["field_name"]: call["field_schema"] for call in raw.create_payload_index_calls}
    assert kinds == {
        "status": ExtendedModels.PayloadSchemaType.KEYWORD,
        "fields.skill_set": ExtendedModels.PayloadSchemaType.KEYWORD,
        "fields.years_of_experience": ExtendedModels.PayloadSchemaType.FLOAT,
    }


@pytest.mark.asyncio
async def test_an_existing_collection_gains_the_declared_field_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = ExtendedRawQdrant()
    raw.existing_payload_schema = {"status": {"data_type": "keyword"}}
    repository, context = _repository(monkeypatch, raw)

    await repository.ensure_index(
        VectorIndexSpec(
            index_name="docs",
            dimension=3,
            metadata_indexes=["status", "fields.skill_set"],
        ),
        context=context,
    )

    assert [call["field_name"] for call in raw.create_payload_index_calls] == ["fields.skill_set"]
    assert raw.delete_payload_index_calls == []


@pytest.mark.asyncio
async def test_indexed_fields_are_cached_and_refreshed_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = ExtendedRawQdrant()
    raw.existing_payload_schema = {"status": {"data_type": "keyword"}}
    repository, context = _repository(monkeypatch, raw)

    first = await repository.indexed_payload_fields("docs", context=context)
    raw.existing_payload_schema = {"status": {}, "fields.skill_set": {}}
    cached = await repository.indexed_payload_fields("docs", context=context)
    refreshed = await repository.indexed_payload_fields("docs", refresh=True, context=context)

    assert first == cached == frozenset({"status"})
    assert refreshed == frozenset({"status", "fields.skill_set"})
