"""Containment runs parent to child, so a structure points at the chunks it holds."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import PROJECTED_RELATION_TYPES, RelationType

pytestmark = pytest.mark.unit


def test_has_chunk_joins_the_parent_to_child_containment_family() -> None:
    # CONTAINS, PARENT_OF, HAS_VERSION and HAS_DATA_SOURCE all run parent -> child.
    # SUPPORTS was the lone child -> parent edge, which is why traversal had to default
    # to BOTH directions to answer the most common question.
    assert RelationType.HAS_CHUNK in PROJECTED_RELATION_TYPES


def test_supports_is_no_longer_projected() -> None:
    assert RelationType.SUPPORTS not in PROJECTED_RELATION_TYPES
