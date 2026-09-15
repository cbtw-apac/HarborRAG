"""Exact semantic projection verification and independent build isolation."""

from copy import deepcopy

import pytest

from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.topology import FalkorTopologyRepository
from harborrag_adapters.repositories.graph.falkordb.topology_mapping import projection_rows
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import (
    CanonicalAssertion,
    CanonicalMention,
    DocumentTopologyBuild,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
)

from ..topology_fixtures import artifact
from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem

CONTEXT = StorageOperationContext.system("tenant-1")


def build() -> DocumentTopologyBuild:
    owner = {
        "tenant_id": "tenant-1",
        "build_id": "build-1",
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "chunk_id": "chunk-1",
    }
    mentions = tuple(
        CanonicalMention(
            **owner,
            mention_id=name,
            entity_id=name,
            observation=ExtractedEntity(
                local_id=name,
                name=name,
                entity_type="service",
                span=EvidenceSpan(start=index, end=index + 1, quote=name),
            ),
        )
        for index, name in enumerate(("A", "B"))
    )
    assertion = CanonicalAssertion(
        **owner,
        assertion_id="claim-1",
        subject_entity_id="A",
        object_entity_id="B",
        observation=ExtractedAssertion(
            local_id="claim-1",
            subject_id="A",
            object_id="B",
            predicate="depends_on",
            polarity="negative",
            modality="possible",
            time_qualifier="after 2025",
            span=EvidenceSpan(start=0, end=2, quote="AB"),
        ),
    )
    return DocumentTopologyBuild(
        build_id="build-1",
        job_id="job-1",
        artifact=artifact(),
        chunk_ids=("chunk-1",),
        mentions=mentions,
        assertions=(assertion,),
    )


def repository(client: FakeFalkorDBClient) -> FalkorTopologyRepository:
    return FalkorTopologyRepository(FalkorDBGraphConfig(), client=client)  # type: ignore[arg-type]


def result(rows: list[dict]) -> FakeQueryResult:
    keys = list(rows[0]) if rows else []
    return FakeQueryResult(
        [HeaderItem(key) for key in keys], [[row.get(key) for key in keys] for row in rows]
    )


def readback(value: DocumentTopologyBuild) -> tuple[list[dict], list[dict]]:
    nodes, edges = projection_rows(value, "tenant-1")
    return [{"properties": deepcopy(row)} for row in nodes], [
        {
            "properties": {
                key: item for key, item in row.items() if key not in {"source", "target"}
            },
            "source": row["source"],
            "target": row["target"],
            "source_tenant": "tenant-1",
            "target_tenant": "tenant-1",
            "source_build": value.build_id,
            "target_build": value.build_id,
        }
        for row in edges
    ]


@pytest.mark.asyncio
async def test_round_trip_verifies_exact_properties_and_qualified_assertions() -> None:
    value = build()
    nodes, edges = readback(value)
    client = FakeFalkorDBClient()
    client.read_results = [result(nodes), result(edges)]
    assert await repository(client).verify(value, context=CONTEXT)
    claim = next(row["properties"] for row in nodes if row["properties"]["kind"] == "assertion")
    assert (claim["polarity"], claim["modality"], claim["time_qualifier"]) == (
        "negative",
        "possible",
        "after 2025",
    )
    assert "quote" not in claim
    assert all(
        parameters == {"tenant_id": "tenant-1", "build_id": "build-1"}
        for _, parameters in client.read_calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "mutated_node",
        "missing_node",
        "duplicate_node",
        "missing_edge",
        "extra_edge",
        "miswired_edge",
        "foreign_tenant",
        "foreign_build",
        "edge_property",
        "malformed_node",
    ],
)
async def test_verification_rejects_projection_corruption(corruption: str) -> None:
    value = build()
    nodes, edges = readback(value)
    if corruption == "mutated_node":
        claim = next(row["properties"] for row in nodes if row["properties"]["kind"] == "assertion")
        claim["polarity"] = "affirmative"  # Keep stored payload_sha256 unchanged.
    elif corruption == "missing_node":
        nodes.pop()
    elif corruption == "duplicate_node":
        nodes.append(nodes[0])
    elif corruption == "missing_edge":
        edges.pop()
    elif corruption == "extra_edge":
        edges.append(deepcopy(edges[0]))
    elif corruption == "miswired_edge":
        edges[0]["target"] = "entity:B"
    elif corruption == "foreign_tenant":
        edges[0]["target_tenant"] = "tenant-2"
    elif corruption == "foreign_build":
        edges[0]["target_build"] = "build-2"
    elif corruption == "edge_property":
        edges[0]["properties"]["unexpected"] = "corrupt"
    else:
        nodes[0] = {"missing": "properties"}
    client = FakeFalkorDBClient()
    client.read_results = [result(nodes), result(edges)]
    assert not await repository(client).verify(value, context=CONTEXT)


@pytest.mark.asyncio
async def test_writes_and_delete_are_tenant_and_build_scoped() -> None:
    client = FakeFalkorDBClient()
    graph = repository(client)
    await graph.write(build(), context=CONTEXT)
    await graph.delete_build("build-1", context=CONTEXT)
    assert len(client.write_calls) == 5
    for statement, parameters in client.write_calls[:2]:
        assert "tenant_id: row.tenant_id" in statement
        assert "build_id: row.build_id" in statement
        assert "KnowledgeNode" not in statement
        assert all(
            row["tenant_id"] == "tenant-1" and row["build_id"] == "build-1"
            for row in parameters["rows"]
        )
    statement, parameters = client.write_calls[-1]
    assert "tenant_id: $tenant_id, build_id: $build_id" in statement
    assert parameters == {"tenant_id": "tenant-1", "build_id": "build-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["tenant", "build", "version", "endpoint", "chunk"])
async def test_foreign_or_missing_support_is_rejected_before_any_write(corruption: str) -> None:
    value = build()
    assertion = value.assertions[0]
    field, replacement = {
        "tenant": ("tenant_id", "tenant-2"),
        "build": ("build_id", "build-2"),
        "version": ("document_version_id", "version-2"),
        "endpoint": ("object_entity_id", "foreign-entity"),
        "chunk": ("chunk_id", "foreign-chunk"),
    }[corruption]
    value = value.model_copy(
        update={"assertions": (assertion.model_copy(update={field: replacement}),)}
    )
    client = FakeFalkorDBClient()
    with pytest.raises(ValueError):
        await repository(client).write(value, context=CONTEXT)
    assert not client.write_calls


@pytest.mark.asyncio
async def test_delete_rejects_empty_build_id() -> None:
    client = FakeFalkorDBClient()
    with pytest.raises(ValueError, match="non-empty"):
        await repository(client).delete_build(" ", context=CONTEXT)
    assert not client.write_calls


@pytest.mark.asyncio
async def test_read_only_connection_does_not_provision_indexes_or_constraints() -> None:
    client = FakeFalkorDBClient()
    await repository(client).connect(provision=False)
    assert not client.write_calls
