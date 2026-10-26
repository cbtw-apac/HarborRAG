"""Derived products must not silently stop running when a new schema version lands."""

from __future__ import annotations

import pytest

from harborrag_core.topology.revisions import (
    _PROJECTION_REVISION_BY_SCHEMA,
    DERIVED_CAPABLE_PROJECTION_REVISIONS,
    projection_revision_for_schema,
)

pytestmark = pytest.mark.unit

# semantic-v1 predates derived products and is deliberately excluded.
_PRE_DERIVED = frozenset({"semantic-v1"})


def test_every_reachable_projection_revision_supports_derived_products() -> None:
    # The allowlist was copied verbatim into three call sites; a new schema version added
    # to the map without extending it silently disables contextual/parent products for
    # every new build, with no error anywhere.
    reachable = set(_PROJECTION_REVISION_BY_SCHEMA.values()) - _PRE_DERIVED

    assert reachable <= DERIVED_CAPABLE_PROJECTION_REVISIONS


def test_the_current_extraction_schema_is_derived_capable() -> None:
    assert projection_revision_for_schema("4") in DERIVED_CAPABLE_PROJECTION_REVISIONS
