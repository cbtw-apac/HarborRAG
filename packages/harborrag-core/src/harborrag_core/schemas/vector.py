from __future__ import annotations

from enum import StrEnum
from math import isfinite
from typing import Any

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import TenantId


def _validate_dense_vector(value: list[float], *, label: str = "") -> list[float]:
    prefix = f"{label} " if label else ""
    if not value:
        raise ValueError(f"{prefix}vector must not be empty")
    if not all(isfinite(item) for item in value):
        raise ValueError(f"{prefix}vector values must be finite")
    return value


class VectorDistance(StrEnum):
    """Enumerates supported vector distance values."""

    COSINE = "cosine"
    DOT_PRODUCT = "dot_product"
    EUCLIDEAN = "euclidean"
    MANHATTAN = "manhattan"


class FilterOperator(StrEnum):
    """Enumerates supported filter operator values."""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    EXISTS = "exists"


class VectorFilterCondition(StrictModel):
    """Provides field condition behavior."""

    field: str = Field(min_length=1)
    operator: FilterOperator = FilterOperator.EQUALS
    value: Any = None


class VectorFilter(StrictModel):
    """Provides vector filter behavior."""

    must: list[VectorFilterCondition] = Field(default_factory=list)
    should: list[VectorFilterCondition] = Field(default_factory=list)
    must_not: list[VectorFilterCondition] = Field(default_factory=list)


class SparseVector(StrictModel):
    """Provides sparse vector behavior."""

    indices: list[int]
    values: list[float]

    @model_validator(mode="after")
    def validate_lengths(self) -> SparseVector:
        if len(self.indices) != len(self.values):
            raise ValueError("sparse indices and values must have equal lengths")
        if any(index < 0 for index in self.indices):
            raise ValueError("sparse indices must be non-negative")
        if self.indices != sorted(self.indices):
            raise ValueError("sparse indices must be sorted")
        if len(set(self.indices)) != len(self.indices):
            raise ValueError("sparse indices must be unique")
        if not all(isfinite(value) and value > 0 for value in self.values):
            raise ValueError("sparse values must be finite and positive")
        return self


class VectorIndexSpec(StrictModel):
    """Describes one logical vector index independently of its backend."""

    index_name: str = Field(min_length=1)
    dimension: int = Field(ge=1)
    distance: VectorDistance = VectorDistance.COSINE
    tenant_scoped: bool = True
    metadata_indexes: list[str] = Field(default_factory=list)
    dense_vector_name: str | None = None
    sparse_vector_name: str | None = None
    sparse_idf: bool = True

    @model_validator(mode="after")
    def validate_vector_names(self) -> VectorIndexSpec:
        if self.sparse_vector_name is not None and self.dense_vector_name is None:
            raise ValueError("a sparse vector collection requires a named dense vector")
        if self.sparse_vector_name == self.dense_vector_name is not None:
            raise ValueError("dense and sparse vector names must differ")
        return self


class VectorIndexRecord(StrictModel):
    """One provider-independent dense/sparse index record."""

    id: str = Field(min_length=1)
    tenant_id: TenantId
    vector: list[float]
    sparse_vector: SparseVector | None = None
    named_vectors: dict[str, list[float]] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("vector")
    @classmethod
    def validate_vector(cls, value: list[float]) -> list[float]:
        return _validate_dense_vector(value)


class VectorSearchQuery(StrictModel):
    """Describes a validated vector search query."""

    index_name: str = Field(min_length=1)
    vector: list[float]
    top_k: int = Field(default=10, ge=1, le=1000)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    filters: VectorFilter | None = None
    include_vectors: bool = False
    include_payload: bool = True
    offset: int = Field(default=0, ge=0)

    @field_validator("vector")
    @classmethod
    def validate_vector(cls, value: list[float]) -> list[float]:
        return _validate_dense_vector(value, label="query")


class SparseSearchQuery(StrictModel):
    """Describes a validated sparse-vector search query."""

    index_name: str = Field(min_length=1)
    sparse_vector: SparseVector
    top_k: int = Field(default=10, ge=1, le=1000)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    filters: VectorFilter | None = None
    include_vectors: bool = False
    include_payload: bool = True
    offset: int = Field(default=0, ge=0)


class HybridSearchQuery(VectorSearchQuery):
    """Describes a validated hybrid search query."""

    sparse_vector: SparseVector
    dense_weight: float = Field(default=0.7, ge=0, le=1)


class VectorSearchResult(StrictModel):
    """Represents vector search result data shared across HarborRAG layers."""

    id: str
    score: float = Field(ge=0, le=1)
    raw_score: float
    payload: dict[str, Any] = Field(default_factory=dict)
    vector: list[float] | None = None


class VectorIndexScanPage(StrictModel):
    """Represents vector scan page data shared across HarborRAG layers."""

    records: list[VectorIndexRecord]
    next_cursor: str | None = None


class VectorStoreCapabilities(StrictModel):
    """Describes supported vector store features."""

    supports_dense_vectors: bool = True
    supports_sparse_vectors: bool = False
    supports_named_vectors: bool = False
    supports_hybrid_search: bool = False
    supports_metadata_filtering: bool = True
    supports_delete_by_filter: bool = False
    supports_full_text_search: bool = False
    supports_quantization: bool = False
    supports_index_aliases: bool = False
    supports_transactions: bool = False
    supports_pagination: bool = True
