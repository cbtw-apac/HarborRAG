from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper

from .fakes import FakeQueryResult, HeaderItem


def test_safe_identifier_accepts_valid_and_rejects_invalid_relationship_types() -> None:
    assert FalkorDBMapper.safe_identifier("knows") == "KNOWS"
    assert FalkorDBMapper.safe_identifier("Related_To_2") == "RELATED_TO_2"

    with pytest.raises(ValueError, match="relationship types must match"):
        FalkorDBMapper.safe_identifier("2bad")

    with pytest.raises(ValueError, match="relationship types must match"):
        FalkorDBMapper.safe_identifier("bad-id")


def test_node_maps_raw_dict_and_pops_harbor_metadata_fields() -> None:
    raw = {
        "id": "n1",
        "tenant_id": "tenant-a",
        "labels": ["Person", "Employee"],
        "confidence": 0.5,
        "provenance": {"source": "test"},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
        "name": "Ada",
    }

    node = FalkorDBMapper.node(raw, "tenant-a")

    assert str(node.id) == "n1"
    assert node.labels == {"Person", "Employee"}
    assert node.confidence == 0.5
    assert node.provenance == {"source": "test"}
    assert node.properties == {"name": "Ada"}


def test_node_falls_back_to_raw_labels_attribute_when_missing_from_properties() -> None:
    class RawNode:
        def __init__(self) -> None:
            self.properties = {
                "id": "n1",
                "tenant_id": "tenant-a",
                "confidence": None,
                "provenance": {},
            }
            self.labels = ["Fallback"]

    node = FalkorDBMapper.node(RawNode(), "tenant-a")

    assert node.labels == {"Fallback"}


def test_edge_prefers_raw_relation_attribute_over_encoded_type_property() -> None:
    class RawEdge:
        def __init__(self) -> None:
            self.properties = {
                "id": "e1",
                "tenant_id": "tenant-a",
                "source_id": "n1",
                "target_id": "n2",
                "type": "IGNORED",
                "confidence": None,
                "provenance": {},
            }
            self.relation = "KNOWS"

    edge = FalkorDBMapper.edge(RawEdge(), "tenant-a")

    assert edge.relationship_type == "KNOWS"
    assert str(edge.source_id) == "n1"
    assert str(edge.target_id) == "n2"


def test_edge_falls_back_to_type_property_and_default_when_raw_is_plain_dict() -> None:
    with_type = {
        "id": "e1",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "type": "KNOWS",
        "confidence": None,
        "provenance": {},
    }
    edge = FalkorDBMapper.edge(with_type, "tenant-a")
    assert edge.relationship_type == "KNOWS"

    without_type = {
        "id": "e2",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "confidence": None,
        "provenance": {},
    }
    edge = FalkorDBMapper.edge(without_type, "tenant-a")
    assert edge.relationship_type == "RELATED_TO"


def test_rows_resolves_header_names_from_tuples_attributes_and_fallback() -> None:
    result = FakeQueryResult(
        header=[(1, "id"), HeaderItem("name"), HeaderItem(None)],
        result_set=[["n1", "Ada", "extra"]],
    )

    rows = FalkorDBMapper.rows(result)

    assert rows == [{"id": "n1", "name": "Ada", "column_2": "extra"}]


def test_node_preserves_a_truthy_valid_to_timestamp() -> None:
    raw = {
        "id": "n1",
        "tenant_id": "tenant-a",
        "labels": ["Person"],
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": "2024-06-01T00:00:00+00:00",
    }

    node = FalkorDBMapper.node(raw, "tenant-a")

    assert node.valid_to is not None
    assert node.valid_to.year == 2024
    assert node.valid_to.month == 6


def test_edge_preserves_a_truthy_valid_to_timestamp() -> None:
    raw: dict[str, Any] = {
        "id": "e1",
        "tenant_id": "tenant-a",
        "source_id": "n1",
        "target_id": "n2",
        "type": "KNOWS",
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": "2024-06-01T00:00:00+00:00",
    }

    edge = FalkorDBMapper.edge(raw, "tenant-a")

    assert edge.valid_to is not None
    assert edge.valid_to.year == 2024
    assert edge.valid_to.month == 6
