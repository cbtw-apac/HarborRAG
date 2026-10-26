"""Semantic-v4 exposes compact entities and one edge per directed pair."""

from copy import deepcopy

import pytest

from harborrag_adapters.repositories.graph.falkordb.unified_topology_builder import (
    UnifiedTopologyBuilder,
)
from harborrag_core.topology import EvidenceSpan

from .fakes import FakeFalkorDBClient
from .test_topology import CONTEXT, repository, result
from .test_typed_topology import typed_build


def unified_build(*, repeated_support: bool = False):
    value = typed_build()
    mentions = tuple(
        mention.model_copy(
            update={
                "observation": mention.observation.model_copy(
                    update={
                        "description": f"{mention.observation.name} is a service.",
                        "description_evidence": (
                            EvidenceSpan(
                                start=mention.observation.span.start,
                                end=mention.observation.span.end,
                                quote=mention.observation.span.quote,
                            ),
                        ),
                    }
                )
            }
        )
        for mention in value.mentions
    )
    assertions = value.assertions
    if repeated_support:
        duplicate_mention = mentions[0].model_copy(update={"mention_id": "A-second"})
        duplicate_assertion = assertions[0].model_copy(update={"assertion_id": "claim-second"})
        mentions = (*mentions, duplicate_mention)
        assertions = (*assertions, duplicate_assertion)
    return value.model_copy(
        update={
            "projection_revision": "semantic-v6",
            "mentions": mentions,
            "assertions": assertions,
        }
    )


def readback(value):
    rows = UnifiedTopologyBuilder().build(value, "tenant-1")
    chunk_fields = ("chunk_id", "name", "description")
    chunks = [{key: row[key] for key in chunk_fields} for row in rows.chunks]
    entities = [{"properties": deepcopy(row)} for row in rows.entities]
    edges = [
        {
            "source": row["source"],
            "target": row["target"],
            "relation_type": row["relation_type"],
            "properties": {
                key: item
                for key, item in row.items()
                if key
                not in {
                    "source",
                    "target",
                    "source_kind",
                    "relation_type",
                    "relation_key",
                    "document_version_id",
                }
            },
        }
        for row in rows.edges
    ]
    return rows, chunks, entities, edges


def test_builder_enriches_structural_chunks_and_describes_entities():
    rows, *_ = readback(unified_build())
    assert rows.chunks[0]["description"] == "Service dependency"
    assert {row["description"] for row in rows.entities} == {
        "A is a service.",
        "B is a service.",
    }
    assert {row["type"] for row in rows.entities} == {"service"}
    assert {row["name"] for row in rows.entities} == {"A", "B"}
    assert all("record_key" not in row and "entity_id" not in row for row in rows.entities)


def test_builder_collapses_repeated_mentions_and_assertions_without_losing_support():
    rows, *_ = readback(unified_build(repeated_support=True))
    assert len(rows.edges) == 3
    mentions = [row for row in rows.edges if row["relation_type"] == "MENTIONS"]
    assertion = next(row for row in rows.edges if row["relation_type"] == "RELATES")
    assert sorted(row["support_count"] for row in mentions) == [1, 2]
    assert assertion["support_count"] == 2
    assert assertion["types"] == ["DEPENDS_ON"]
    assert assertion["description"] == "A may not depend on B after 2025 except on approval."
    assert assertion["claim_count"] == 1
    assert assertion["conflict_count"] == 0
    assert assertion["has_conflicts"] is False
    assert "polarities" not in assertion and "modalities" not in assertion
    assert "assertion_ids" not in assertion and "support_chunk_ids" not in assertion


def test_builder_reports_conflicting_claims_without_inventing_qualifier_combinations():
    value = unified_build()
    original = value.assertions[0]
    conflicting = original.model_copy(
        update={
            "assertion_id": "affirmative-claim",
            "observation": original.observation.model_copy(
                update={
                    "local_id": "affirmative-claim",
                    "polarity": "affirmative",
                    "modality": "asserted",
                    "statement_text": "A depends on B.",
                    "qualifiers": (),
                }
            ),
        }
    )
    rows = UnifiedTopologyBuilder().build(
        value.model_copy(update={"assertions": (*value.assertions, conflicting)}), "tenant-1"
    )
    relation = next(row for row in rows.edges if row["relation_type"] == "RELATES")

    assert relation["claim_count"] == 2
    assert relation["conflict_count"] == 1
    assert relation["has_conflicts"] is True
    assert relation["description"].startswith("2 evidence-backed claims conflict")
    assert "polarities" not in relation and "modalities" not in relation


@pytest.mark.asyncio
async def test_projection_writes_real_chunks_and_verifies_exact_consolidated_graph():
    value = unified_build(repeated_support=True)
    rows, chunks, entities, edges = readback(value)
    client = FakeFalkorDBClient()
    graph = repository(client)
    await graph.write(value, context=CONTEXT)
    assert "record_key IS NOT NULL" in client.write_calls[0][0]
    chunk_statement, chunk_parameters = client.write_calls[1]
    assert "MATCH (chunk:KnowledgeNode:Chunk" in chunk_statement
    assert "chunk.title = row.name" in chunk_statement
    assert "chunk.title_key = toLower(row.name)" in chunk_statement
    assert "retrieval_context" not in chunk_statement
    assert chunk_parameters["rows"] == rows.chunks
    structure_statement, structure_parameters = client.write_calls[2]
    assert "collect(chunk) AS children" in structure_statement
    assert "size(children) = 1" in structure_statement
    assert "structure.entity_type = 'table'" in structure_statement
    assert structure_parameters == {
        "tenant_id": "tenant-1",
        "document_version_id": value.document_version_id,
        "build_id": value.build_id,
    }
    entity_statement, entity_parameters = client.write_calls[3]
    assert "TopologyRecord:Entity" in entity_statement
    assert entity_parameters["rows"] == rows.entities
    statements = [statement for statement, _ in client.write_calls[4:]]
    assert sum("[r:MENTIONS" in statement for statement in statements) == 1
    assert sum("[r:RELATES" in statement for statement in statements) == 1
    client.read_results = [result(chunks), result([]), result(entities), result(edges)]
    assert await graph.verify(value, context=CONTEXT)


@pytest.mark.asyncio
async def test_projection_detects_missing_description_and_parallel_edge_corruption():
    value = unified_build()
    _, chunks, entities, edges = readback(value)
    chunks[0]["description"] = None
    edges.append(deepcopy(edges[0]))
    client = FakeFalkorDBClient()
    client.read_results = [result(chunks), result([]), result(entities), result(edges)]
    assert not await repository(client).verify(value, context=CONTEXT)


@pytest.mark.asyncio
async def test_projection_detects_ambiguous_or_missing_leaf_structure_description():
    value = unified_build()
    _, chunks, entities, edges = readback(value)
    structures = [
        {
            "child_count": 2,
            "occurrences": 2,
            "child_description": "Service dependency",
            "structure_description": "Service dependency",
            "description_build_id": value.build_id,
        }
    ]
    client = FakeFalkorDBClient()
    client.read_results = [
        result(chunks),
        result(structures),
        result(entities),
        result(edges),
    ]
    assert not await repository(client).verify(value, context=CONTEXT)
