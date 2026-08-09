from __future__ import annotations

from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborStorageCapabilityError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_core.indexing import VectorIndexSpec


def collection_spec_from_qdrant(
    info: Any,
    *,
    collection: str,
    error_context: StorageErrorContext,
    models: Any,
) -> VectorIndexSpec:
    vectors = info.config.params.vectors
    if not isinstance(vectors, dict):
        return VectorIndexSpec(
            index_name=collection,
            dimension=int(vectors.size),
            distance=QdrantMapper.vector_distance(vectors.distance, models),
        )
    if len(vectors) != 1:
        raise HarborStorageCapabilityError(
            f"collection schema {collection!r} must have one dense vector",
            context=error_context,
        )
    dense_name, dense = next(iter(vectors.items()))
    sparse_vectors = getattr(info.config.params, "sparse_vectors", None) or {}
    sparse_name = next(iter(sparse_vectors)) if len(sparse_vectors) == 1 else None
    sparse_config = sparse_vectors.get(sparse_name) if sparse_name is not None else None
    modifier = getattr(sparse_config, "modifier", None)
    return VectorIndexSpec(
        index_name=collection,
        dimension=int(dense.size),
        distance=QdrantMapper.vector_distance(dense.distance, models),
        dense_vector_name=dense_name,
        sparse_vector_name=sparse_name,
        sparse_idf=modifier == models.Modifier.IDF,
    )
