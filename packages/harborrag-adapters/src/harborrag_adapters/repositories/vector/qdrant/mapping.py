from __future__ import annotations

import math
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from harborrag_core.indexing import (
    FilterOperator,
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
    VectorIndexRecord,
)


class QdrantMapper:
    """Translates HarborRAG vector schemas into Qdrant SDK objects."""

    @classmethod
    def point_id(cls, logical_id: str) -> UUID:
        """Map a canonical point identity to one stable Qdrant UUID."""

        try:
            return UUID(logical_id)
        except ValueError:
            pass
        return uuid5(NAMESPACE_URL, f"harborrag:qdrant:{logical_id}")

    @classmethod
    def point(cls, point: VectorIndexRecord, vector: Any, models: Any) -> Any:
        """Map one validated projection point without decorating its payload."""

        return models.PointStruct(
            id=cls.point_id(point.id),
            vector=vector,
            payload=dict(point.payload),
        )

    @classmethod
    def distance(cls, distance: VectorDistance, models: Any) -> Any:
        """Map a portable distance metric to the matching Qdrant value."""
        mapping = {
            VectorDistance.COSINE: models.Distance.COSINE,
            VectorDistance.DOT_PRODUCT: models.Distance.DOT,
            VectorDistance.EUCLIDEAN: models.Distance.EUCLID,
            VectorDistance.MANHATTAN: models.Distance.MANHATTAN,
        }
        return mapping[distance]

    @classmethod
    def vector_distance(cls, distance: Any, models: Any) -> VectorDistance:
        """Map a persisted Qdrant distance value back to the portable schema."""
        mapping = {
            models.Distance.COSINE: VectorDistance.COSINE,
            models.Distance.DOT: VectorDistance.DOT_PRODUCT,
            models.Distance.EUCLID: VectorDistance.EUCLIDEAN,
            models.Distance.MANHATTAN: VectorDistance.MANHATTAN,
        }
        return mapping[distance]

    @classmethod
    def filter(
        cls,
        filter_value: VectorFilter | None,
        models: Any,
    ) -> Any | None:
        """Build a provider filter inside an already tenant-scoped collection."""
        if filter_value is None:
            return None
        return models.Filter(
            must=[cls.condition(item, models) for item in filter_value.must] or None,
            should=[cls.condition(item, models) for item in filter_value.should] or None,
            must_not=[cls.condition(item, models) for item in filter_value.must_not] or None,
        )

    @classmethod
    def condition(cls, condition: VectorFilterCondition, models: Any) -> Any:
        """Translate one typed filter condition without string interpolation."""
        if condition.operator == FilterOperator.EQUALS:
            return models.FieldCondition(
                key=condition.field, match=models.MatchValue(value=condition.value)
            )
        if condition.operator == FilterOperator.IN:
            return models.FieldCondition(
                key=condition.field, match=models.MatchAny(any=condition.value)
            )
        if condition.operator == FilterOperator.EXISTS:
            is_null = models.IsNullCondition(is_null=models.PayloadField(key=condition.field))
            if condition.value is False:
                return is_null
            return models.Filter(must_not=[is_null])
        ranges = {
            FilterOperator.GREATER_THAN: "gt",
            FilterOperator.GREATER_THAN_OR_EQUAL: "gte",
            FilterOperator.LESS_THAN: "lt",
            FilterOperator.LESS_THAN_OR_EQUAL: "lte",
        }
        if condition.operator in ranges:
            return models.FieldCondition(
                key=condition.field,
                range=models.Range(**{ranges[condition.operator]: condition.value}),
            )
        if condition.operator in {FilterOperator.NOT_EQUALS, FilterOperator.NOT_IN}:
            matcher = (
                models.MatchValue(value=condition.value)
                if condition.operator == FilterOperator.NOT_EQUALS
                else models.MatchAny(any=condition.value)
            )
            return models.Filter(
                must_not=[models.FieldCondition(key=condition.field, match=matcher)]
            )
        raise ValueError(f"unsupported Qdrant filter operator {condition.operator}")

    @classmethod
    def normalize_score(cls, raw: float, distance: VectorDistance) -> float:
        """Return a bounded monotonic public score in the range zero to one."""
        if distance == VectorDistance.COSINE:
            return max(0.0, min(1.0, (raw + 1.0) / 2.0))
        if distance == VectorDistance.DOT_PRODUCT:
            if raw >= 0:
                return 1.0 / (1.0 + math.exp(-min(raw, 700)))
            exponent = math.exp(max(raw, -700))
            return exponent / (1.0 + exponent)
        return 1.0 / (1.0 + abs(raw))
