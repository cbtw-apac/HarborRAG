from __future__ import annotations

import pytest

from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    FilterOperator,
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
)

from .fakes import Condition, ExtendedModels, FakeModels


def test_distance_and_vector_distance_round_trip_all_metrics() -> None:
    for distance in VectorDistance:
        provider_value = QdrantMapper.distance(distance, FakeModels)
        assert QdrantMapper.vector_distance(provider_value, FakeModels) == distance


def test_point_id_is_stable_and_namespaced_per_tenant() -> None:
    first = QdrantMapper.point_id("tenant-a", "doc-1")
    second = QdrantMapper.point_id("tenant-a", "doc-1")
    other_tenant = QdrantMapper.point_id("tenant-b", "doc-1")
    assert first == second
    assert first != other_tenant


def test_filter_without_value_only_scopes_tenant() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    result = QdrantMapper.filter(None, context, ExtendedModels)
    assert len(result.must) == 1
    assert result.must[0].key == "_harbor_tenant_id"
    assert result.must[0].match.value == "tenant-a"


def test_filter_combines_must_should_and_must_not() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    filter_value = VectorFilter(
        must=[VectorFilterCondition(field="a", operator=FilterOperator.EQUALS, value=1)],
        should=[VectorFilterCondition(field="b", operator=FilterOperator.EQUALS, value=2)],
        must_not=[VectorFilterCondition(field="c", operator=FilterOperator.EQUALS, value=3)],
    )

    result = QdrantMapper.filter(filter_value, context, ExtendedModels)

    assert len(result.must) == 2  # tenant scope + explicit "must" condition
    assert len(result.should) == 1
    assert len(result.must_not) == 1


def test_condition_covers_every_supported_filter_operator() -> None:
    equals = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.EQUALS, value=1), ExtendedModels
    )
    assert equals.match.value == 1

    in_condition = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.IN, value=[1, 2]), ExtendedModels
    )
    assert in_condition.match.any == [1, 2]

    exists_true = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.EXISTS, value=True),
        ExtendedModels,
    )
    assert exists_true.must_not[0].is_null.key == "a"

    exists_false = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.EXISTS, value=False),
        ExtendedModels,
    )
    assert exists_false.is_null.key == "a"

    greater_than = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.GREATER_THAN, value=5),
        ExtendedModels,
    )
    assert greater_than.range.gt == 5

    greater_or_equal = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.GREATER_THAN_OR_EQUAL, value=5),
        ExtendedModels,
    )
    assert greater_or_equal.range.gte == 5

    less_than = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.LESS_THAN, value=5),
        ExtendedModels,
    )
    assert less_than.range.lt == 5

    less_or_equal = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.LESS_THAN_OR_EQUAL, value=5),
        ExtendedModels,
    )
    assert less_or_equal.range.lte == 5

    not_equals = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.NOT_EQUALS, value=1),
        ExtendedModels,
    )
    assert not_equals.must_not[0].match.value == 1

    not_in = QdrantMapper.condition(
        Condition(field="a", operator=FilterOperator.NOT_IN, value=[1, 2]),
        ExtendedModels,
    )
    assert not_in.must_not[0].match.any == [1, 2]

    with pytest.raises(ValueError, match="unsupported Qdrant filter operator"):
        QdrantMapper.condition(Condition(field="a", operator="unsupported"), ExtendedModels)


def test_normalize_score_covers_every_distance_branch() -> None:
    assert QdrantMapper.normalize_score(1.0, VectorDistance.COSINE) == 1.0
    assert QdrantMapper.normalize_score(-3.0, VectorDistance.COSINE) == 0.0
    assert 0.9 < QdrantMapper.normalize_score(5.0, VectorDistance.DOT_PRODUCT) < 1.0
    assert 0.0 < QdrantMapper.normalize_score(-5.0, VectorDistance.DOT_PRODUCT) < 0.1
    assert QdrantMapper.normalize_score(0.0, VectorDistance.EUCLIDEAN) == 1.0
    assert 0.0 < QdrantMapper.normalize_score(1.0, VectorDistance.MANHATTAN) < 1.0
