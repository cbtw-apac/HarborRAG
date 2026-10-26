"""Every index a derived product can publish to must be one cleanup can reclaim."""

from __future__ import annotations

import pytest

from harborrag_core.topology.derived import (
    DERIVED_VECTOR_PRODUCTS,
    ContextualIndexProfile,
)
from harborrag_runtime.topology.derived_cleanup import _INDEX_PREFIXES

pytestmark = pytest.mark.unit


def _profile() -> ContextualIndexProfile:
    return ContextualIndexProfile(
        model="embed-1",
        dimension=8,
        deployment_revision="deployment-1",
        description_revision="desc-1",
    )


def test_every_publishable_index_prefix_is_known_to_cleanup() -> None:
    # Three uncoordinated dicts governed these names; forgetting one leaks vector points
    # with no error at write, read or cleanup time.
    assert {product.index_prefix for product in DERIVED_VECTOR_PRODUCTS} == set(
        _INDEX_PREFIXES.values()
    )


def test_registry_reproduces_the_profile_index_names_exactly() -> None:
    profile = _profile()
    names = {
        product.artifact_kind: product.index_name(product.fingerprint(profile))
        for product in DERIVED_VECTOR_PRODUCTS
    }

    assert names["contextual_chunk"] == profile.index_name
    assert names["parent_description"] == profile.parent_index_name


def test_artifact_kind_and_record_kind_are_distinct_vocabularies() -> None:
    # contextual publishes artifact_kind "contextual_chunk" but record_kind "contextual";
    # conflating them silently breaks payload validation.
    contextual = next(p for p in DERIVED_VECTOR_PRODUCTS if p.artifact_kind == "contextual_chunk")

    assert contextual.record_kind == "contextual"
