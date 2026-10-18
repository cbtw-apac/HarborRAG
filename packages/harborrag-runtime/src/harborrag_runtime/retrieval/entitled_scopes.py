"""The vector-filter form of a principal's datasource entitlement."""

from __future__ import annotations

from harborrag_core.indexing import FilterOperator, VectorFilterCondition


def entitled_vector_conditions(
    entitled: frozenset[str] | None,
) -> tuple[VectorFilterCondition, ...]:
    """Conditions that rank only inside the datasources ``entitled`` names.

    Every evidence, entity and generated-view point carries ``source_scope_id``, so this
    keeps a ranking window from filling with points the final document check would drop.
    ``None`` is no entitlement set and no condition; the values are sorted so one
    entitlement is always one filter.
    """

    if entitled is None:
        return ()
    return (
        VectorFilterCondition(
            field="source_scope_id", operator=FilterOperator.IN, value=sorted(entitled)
        ),
    )
