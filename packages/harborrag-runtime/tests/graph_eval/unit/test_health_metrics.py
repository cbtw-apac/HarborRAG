from __future__ import annotations

import pytest

from ..health.metrics import (
    GraphHealthReport,
    compute_report,
    connected_component_sizes,
    publication_completeness,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _healthy_report(**overrides: object) -> GraphHealthReport:
    arguments: dict = {
        "tenant_id": "tenant-1",
        "node_census": [
            {"kind": "Tenant", "entity_type": "tenant", "item_count": 1},
            {"kind": "DataSource", "entity_type": "data_source", "item_count": 1},
            {"kind": "SourceEntity", "entity_type": "local_file", "item_count": 3},
            {"kind": "DocumentVersion", "entity_type": "document_version", "item_count": 2},
            {"kind": "Structure", "entity_type": "section", "item_count": 4},
            {"kind": "Chunk", "entity_type": "chunk", "item_count": 6},
        ],
        "relation_census": [
            {
                "source_kind": "Tenant",
                "relation_type": "has_data_source",
                "target_kind": "DataSource",
                "item_count": 1,
            },
            {
                "source_kind": "Chunk",
                "relation_type": "supports",
                "target_kind": "Structure",
                "item_count": 6,
            },
        ],
        "orphan_census": [{"kind": "SourceEntity", "item_count": 1}],
        "placeholder_count": 1,
        "duplicate_semantic_count": 0,
        "top_hubs": [
            {"node_key": "k1", "kind": "Structure", "title": "Operations", "degree": 9},
        ],
        "component_sizes": (16,),
    }
    arguments.update(overrides)
    return compute_report(**arguments)


def test_healthy_graph_passes_all_gates() -> None:
    report = _healthy_report()
    assert report.gate_failures == ()
    assert report.node_count == 17
    assert report.relation_count == 7
    assert report.orphan_source_entity_count == 1
    assert report.placeholder_count == 1


def test_empty_graph_fails_gate() -> None:
    report = _healthy_report(
        node_census=[],
        relation_census=[],
        orphan_census=[],
        top_hubs=[],
        placeholder_count=0,
        component_sizes=(),
    )
    assert report.node_count == 0
    assert any("empty" in failure for failure in report.gate_failures)


def test_unknown_relation_type_fails_gate() -> None:
    report = _healthy_report(
        relation_census=[
            {
                "source_kind": "SourceEntity",
                "relation_type": "mystery_edge",
                "target_kind": "SourceEntity",
                "item_count": 2,
            }
        ]
    )
    assert any("mystery_edge" in failure for failure in report.gate_failures)


def test_unknown_node_kind_fails_gate() -> None:
    report = _healthy_report(node_census=[{"kind": "Blob", "entity_type": "blob", "item_count": 1}])
    assert any("Blob" in failure for failure in report.gate_failures)


def test_orphan_version_owned_nodes_fail_gate() -> None:
    report = _healthy_report(orphan_census=[{"kind": "Chunk", "item_count": 2}])
    assert any("orphan" in failure and "Chunk" in failure for failure in report.gate_failures)


def test_duplicate_semantic_relations_fail_gate() -> None:
    report = _healthy_report(duplicate_semantic_count=3)
    assert any("duplicate" in failure for failure in report.gate_failures)


def test_report_serializes_to_plain_dict() -> None:
    payload = _healthy_report().as_dict()
    assert payload["tenant_id"] == "tenant-1"
    assert payload["gate_failures"] == []
    assert payload["signature_census"]["Chunk supports Structure"] == 6


def test_as_dict_returns_copies_not_references() -> None:
    report = _healthy_report()
    payload = report.as_dict()
    payload["nodes_by_kind"]["Tenant"] = 99
    assert report.nodes_by_kind["Tenant"] == 1


def test_average_degree_derived_from_censuses() -> None:
    report = _healthy_report()
    assert report.average_degree == 14 / 17
    assert report.as_dict()["average_degree"] == 14 / 17


def test_component_sizes_ignore_singletons() -> None:
    sizes = connected_component_sizes(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("d", "e")],
    )
    assert sizes == (3, 2)


def test_publication_completeness_gates_only_on_missing_published_versions() -> None:
    census, failures = publication_completeness(
        {"document:a": "version:a1", "document:b": "version:b2"},
        ["version:a1", "version:b1", "version:b2"],
    )
    assert census == {
        "published_count": 2,
        "missing_count": 0,
        "missing": [],
        "graph_only_version_count": 1,
    }
    assert failures == []


def test_publication_completeness_fails_when_a_published_version_has_no_node() -> None:
    census, failures = publication_completeness(
        {"document:a": "version:a1"},
        ["version:zz"],
    )
    assert census["missing_count"] == 1
    assert census["missing"] == ["document:a -> version:a1"]
    assert failures == ["published versions missing from graph: 1 (e.g. document:a -> version:a1)"]
