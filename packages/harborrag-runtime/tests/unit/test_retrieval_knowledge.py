"""Canonical knowledge reads remain active-version and evidence authoritative."""

from __future__ import annotations

import pytest
from topology_retrieval_support import (
    ActiveVersions,
    CanonicalTopology,
    TopologyVectors,
    service,
)

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.security import AccessContext
from harborrag_runtime.contracts import SemanticPathRequest

ACCESS = AccessContext.system("tenant-1")


@pytest.mark.asyncio
async def test_fetch_evidence_restores_request_order_and_rejects_missing_chunks() -> None:
    response = await service(CanonicalTopology()).fetch_evidence(
        ("chunk-2", "missing", "chunk-1"), access=ACCESS
    )

    assert [item.id for item in response.results] == ["chunk-2", "chunk-1"]
    assert response.unavailable_chunk_ids == ("missing",)
    assert response.results[0].metadata["retrieval_source"] == "qdrant-authoritative"


@pytest.mark.asyncio
async def test_fetch_evidence_rechecks_active_document_versions() -> None:
    authority = ActiveVersions()
    authority.versions["document-2"] = "version-3"

    response = await service(CanonicalTopology(), authority=authority).fetch_evidence(
        ("chunk-1", "chunk-2"), access=ACCESS
    )

    assert [item.id for item in response.results] == ["chunk-1"]
    assert response.unavailable_chunk_ids == ("chunk-2",)


@pytest.mark.asyncio
async def test_entity_resolution_and_relation_direction_use_canonical_topology() -> None:
    runtime = service(CanonicalTopology())

    resolved = await runtime.resolve_entities("alpha", limit=5, access=ACCESS)
    outgoing = await runtime.find_semantic_relations(
        "entity-1", predicates=("depends_on",), direction="outgoing", limit=5, access=ACCESS
    )
    incoming = await runtime.find_semantic_relations(
        "entity-1", predicates=(), direction="incoming", limit=5, access=ACCESS
    )

    assert [item.entity_id for item in resolved.mentions] == ["entity-1"]
    assert [item.assertion_id for item in outgoing.assertions] == ["assertion-1"]
    assert {item.entity_id for item in outgoing.mentions} == {"entity-1", "entity-2"}
    assert incoming.assertions == ()


@pytest.mark.asyncio
async def test_semantic_paths_preserve_direction_and_allow_explicit_reverse_traversal() -> None:
    runtime = service(CanonicalTopology())
    forward = await runtime.find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-1", "entity-2", traversal="directed")
    )
    reverse_directed = await runtime.find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-2", "entity-1", traversal="directed")
    )
    reverse_either = await runtime.find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-2", "entity-1", traversal="either")
    )

    assert [[item.assertion_id for item in path] for path in forward.paths] == [
        ["assertion-1"]
    ]
    assert reverse_directed.paths == ()
    assert [[item.assertion_id for item in path] for path in reverse_either.paths] == [
        ["assertion-1"]
    ]


@pytest.mark.asyncio
async def test_semantic_reads_fail_explicitly_without_canonical_topology() -> None:
    runtime = service(None, TopologyVectors())

    with pytest.raises(HarborCapabilityError, match="semantic topology"):
        await runtime.resolve_entities("Alpha", limit=5, access=ACCESS)
