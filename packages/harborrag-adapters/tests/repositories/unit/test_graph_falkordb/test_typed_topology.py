"""The v2 projection preserves typed qualified evidence without property-node fanout."""

from copy import deepcopy

import pytest

from harborrag_adapters.repositories.graph.falkordb.typed_topology import TypedTopologyBuilder
from harborrag_core.topology.derived import ChunkEnrichment

from .fakes import FakeFalkorDBClient
from .test_topology import CONTEXT, build, repository, result


def typed_build():
    value = build()
    assertion = value.assertions[0]
    observation = assertion.observation.model_copy(
        update={
            "statement_text": "A may not depend on B after 2025 except on approval.",
            "qualifiers": ("except on approval",),
            "attribution": "Team Z",
            "valid_from": "2026",
            "temporal_precision": "year",
        }
    )
    return value.model_copy(
        update={
            "document_id": "doc-1",
            "document_version_id": "version-1",
            "source_scope_id": "source-1",
            "config_epoch": 2,
            "projection_revision": "semantic-v2",
            "assertions": (assertion.model_copy(update={"observation": observation}),),
            "representations": (
                ChunkEnrichment(
                    chunk_id="chunk-1",
                    title="Services",
                    description="Service dependency",
                    retrieval_context="A and B service definitions",
                ),
            ),
        }
    )


def readback(value):
    rows = TypedTopologyBuilder().build(value, "tenant-1")
    nodes = [{"properties": deepcopy(row)} for row in rows.nodes]
    edges = [
        {
            "properties": {
                key: item
                for key, item in row.items()
                if key not in {"source", "target", "relation_type"}
            },
            **{key: row[key] for key in ("source", "target", "relation_type")},
            "source_tenant": "tenant-1",
            "target_tenant": "tenant-1",
            "source_build": value.build_id,
            "target_build": value.build_id,
        }
        for row in rows.edges
    ]
    return nodes, edges


@pytest.mark.asyncio
async def test_repository_v2_writes_incidence_and_typed_edges_with_qualified_provenance():
    value = typed_build()
    client = FakeFalkorDBClient()
    graph = repository(client)
    await graph.write(value, context=CONTEXT)
    nodes = client.write_calls[0][1]["rows"]
    assert {row["kind"] for row in nodes} == {"chunk", "entity_view"}
    assert len(nodes) == 3
    statements = [statement for statement, _ in client.write_calls[1:]]
    assert any("[r:MENTIONS" in statement for statement in statements)
    assert any("[r:DEPENDS_ON" in statement for statement in statements)
    assert all("TOPOLOGY_LINK" not in statement for statement in statements)
    relation = next(
        parameters["rows"][0]["properties"]
        for statement, parameters in client.write_calls
        if "[r:DEPENDS_ON" in statement
    )
    assert relation["polarity"] == "negative" and relation["modality"] == "possible"
    assert relation["attribution"] == "Team Z" and relation["valid_from"] == "2026"
    assert relation["qualifiers"] == '["except on approval"]'
    assert relation["chunk_id"] == "chunk-1" and relation["assertion_id"] == "claim-1"
    assert relation["document_version_id"] == "version-1" and relation["config_epoch"] == 2
    assert "statement_text" not in relation  # Complete assertion remains canonical sidecar.
    actual_nodes, actual_edges = readback(value)
    client.read_results = [result(actual_nodes), result(actual_edges)]
    assert await graph.verify(value, context=CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "node_property",
        "edge_property",
        "missing_edge",
        "extra_edge",
        "foreign_endpoint",
        "wrong_type",
        "missing_node",
    ],
)
async def test_v2_verification_detects_exact_projection_corruption(corruption):
    value = typed_build()
    nodes, edges = readback(value)
    if corruption == "node_property":
        nodes[0]["properties"]["config_epoch"] = 99
    elif corruption == "edge_property":
        edges[0]["properties"]["polarity"] = "affirmative"
    elif corruption == "missing_edge":
        edges.pop()
    elif corruption == "extra_edge":
        edges.append(deepcopy(edges[0]))
    elif corruption == "foreign_endpoint":
        edges[0]["target_tenant"] = "foreign"
    elif corruption == "wrong_type":
        edges[0]["relation_type"] = "OWNS"
    else:
        nodes.pop()
    client = FakeFalkorDBClient()
    client.read_results = [result(nodes), result(edges)]
    assert not await repository(client).verify(value, context=CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,replacement",
    [
        ("document_id", "foreign-doc"),
        ("document_version_id", "foreign-version"),
        ("chunk_id", "foreign-chunk"),
        ("object_entity_id", "missing-entity"),
    ],
)
async def test_v2_rejects_observations_outside_declared_source_manifest(field, replacement):
    value = typed_build()
    claim = value.assertions[0].model_copy(update={field: replacement})
    value = value.model_copy(update={"assertions": (claim,)})
    client = FakeFalkorDBClient()
    with pytest.raises(ValueError):
        await repository(client).write(value, context=CONTEXT)
    assert not client.write_calls


@pytest.mark.asyncio
async def test_v2_rejects_unsafe_predicate_before_cypher_interpolation():
    value = typed_build()
    claim = value.assertions[0]
    malicious = claim.observation.model_copy(update={"predicate": "owns]->() DELETE n //"})
    value = value.model_copy(
        update={"assertions": (claim.model_copy(update={"observation": malicious}),)}
    )
    client = FakeFalkorDBClient()
    with pytest.raises(ValueError, match="relationship identifier"):
        await repository(client).write(value, context=CONTEXT)
    assert not client.write_calls


def test_v2_rejects_conflicting_entity_types_and_duplicate_representations():
    value = typed_build()
    first, second = value.mentions
    second = second.model_copy(
        update={
            "entity_id": first.entity_id,
            "observation": second.observation.model_copy(update={"entity_type": "person"}),
        }
    )
    with pytest.raises(ValueError, match="incompatible"):
        TypedTopologyBuilder().build(
            value.model_copy(update={"mentions": (first, second)}), "tenant-1"
        )
    with pytest.raises(ValueError, match="representations"):
        TypedTopologyBuilder().build(
            value.model_copy(update={"representations": value.representations * 2}), "tenant-1"
        )


def test_v2_entity_views_are_deterministic_under_mention_input_order():
    value = typed_build()
    first = TypedTopologyBuilder().build(value, "tenant-1")
    second = TypedTopologyBuilder().build(
        value.model_copy(update={"mentions": tuple(reversed(value.mentions))}), "tenant-1"
    )
    assert first == second
