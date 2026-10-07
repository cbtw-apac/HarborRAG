"""The retrieval ``filters`` mapping becomes typed payload conditions."""

import pytest

from harborrag_core.indexing import FilterOperator
from harborrag_runtime.retrieval.facades import _build_vector_filter


def test_scalars_lists_and_bounds_map_to_equality_membership_and_ranges() -> None:
    built = _build_vector_filter(
        {
            "fields.skill_set": "Data Engineering",
            "fields.position_level": ["Medior", "Senior"],
            "fields.years_of_experience": {"gte": 3, "lt": 10},
        }
    )

    assert built is not None
    conditions = {(c.field, c.operator): c.value for c in built.must}
    assert conditions == {
        ("fields.position_level", FilterOperator.IN): ["Medior", "Senior"],
        ("fields.skill_set", FilterOperator.EQUALS): "Data Engineering",
        ("fields.years_of_experience", FilterOperator.GREATER_THAN_OR_EQUAL): 3,
        ("fields.years_of_experience", FilterOperator.LESS_THAN): 10,
    }
    assert _build_vector_filter({}) is None


@pytest.mark.parametrize(
    "bounds",
    ({"between": 3}, {"gte": "3"}, {"gte": True}, {}),
)
def test_a_malformed_range_is_rejected_rather_than_matched_as_a_value(
    bounds: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="range"):
        _build_vector_filter({"fields.years_of_experience": bounds})
