"""Canonical knowledge reads remain active-version and evidence authoritative."""

from __future__ import annotations

import asyncio

import pytest
from topology_retrieval_support import (
    ActiveVersions,
    CanonicalTopology,
    TopologyVectors,
    service,
)

from harborrag_core.contracts.errors import HarborCapabilityError, HarborDeadlineExceeded
from harborrag_core.security import AccessContext
from harborrag_core.topology.extraction import EvidenceSpan, ExtractedAssertion
from harborrag_core.topology.records import CanonicalAssertion
from harborrag_runtime.contracts import SemanticPathRequest
from harborrag_runtime.retrieval import knowledge as knowledge_module

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

    assert [[item.assertion_id for item in path] for path in forward.paths] == [["assertion-1"]]
    assert reverse_directed.paths == ()
    assert [[item.assertion_id for item in path] for path in reverse_either.paths] == [
        ["assertion-1"]
    ]


@pytest.mark.asyncio
async def test_semantic_reads_fail_explicitly_without_canonical_topology() -> None:
    runtime = service(None, TopologyVectors())

    with pytest.raises(HarborCapabilityError, match="semantic topology"):
        await runtime.resolve_entities("Alpha", limit=5, access=ACCESS)


def _link(subject: str, object_: str) -> CanonicalAssertion:
    return CanonicalAssertion(
        assertion_id=f"{subject}->{object_}",
        tenant_id="tenant-1",
        build_id="build-1",
        document_id="document-1",
        document_version_id="version-1",
        chunk_id="chunk-1",
        subject_entity_id=subject,
        object_entity_id=object_,
        observation=ExtractedAssertion(
            local_id=f"{subject}-{object_}",
            subject_id=subject,
            object_id=object_,
            predicate="depends_on",
            span=EvidenceSpan(start=0, end=22, quote="Alpha depends on Beta."),
        ),
    )


class DenseTopology(CanonicalTopology):
    """Every entity linked to every other, and the requested end nowhere in it."""

    def __init__(self, size: int) -> None:
        super().__init__()
        names = [f"entity-{index:03}" for index in range(size)]
        self.assertions = tuple(  # type: ignore[assignment]
            _link(left, right) for i, left in enumerate(names) for right in names[i + 1 :]
        )
        self.reads: list[str] = []

    async def active_assertions(self, tenant_id, *, entity_ids=(), limit=100, access=None):  # type: ignore[no-untyped-def]
        self.reads.extend(entity_ids)
        return await super().active_assertions(
            tenant_id, entity_ids=entity_ids, limit=limit, access=access
        )


@pytest.mark.asyncio
async def test_semantic_paths_read_each_entity_once_and_cap_the_queue() -> None:
    topology = DenseTopology(30)

    response = await service(topology).find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-000", "entity-missing", max_hops=3)
    )

    assert response.paths == ()
    assert len(topology.reads) == len(set(topology.reads)), "each entity is read at most once"
    assert response.truncated is True, "29 * 28 second-hop routes exceed the queue cap"


@pytest.mark.asyncio
async def test_semantic_paths_stop_reading_at_the_expansion_budget() -> None:
    topology = DenseTopology(70)

    response = await service(topology).find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-000", "entity-missing", max_hops=3)
    )

    assert len(set(topology.reads)) <= knowledge_module._MAX_PATH_EXPANSIONS
    assert response.truncated is True


@pytest.mark.asyncio
async def test_semantic_paths_give_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class StalledTopology(CanonicalTopology):
        async def active_assertions(self, tenant_id, *, entity_ids=(), limit=100, access=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(1)
            return ()

    monkeypatch.setattr(knowledge_module, "_PATH_SEARCH_SECONDS", 0.01)

    with pytest.raises(HarborDeadlineExceeded):
        await service(StalledTopology()).find_semantic_paths(
            SemanticPathRequest(ACCESS, "entity-1", "entity-2")
        )


@pytest.mark.asyncio
async def test_a_shared_intermediate_still_yields_every_distinct_path() -> None:
    # entity-000 reaches entity-003 through 001 and through 002; the read memo
    # must not prune the second route, only the re-read.
    topology = CanonicalTopology()
    topology.assertions = (  # type: ignore[assignment]
        _link("entity-000", "entity-001"),
        _link("entity-000", "entity-002"),
        _link("entity-001", "entity-003"),
        _link("entity-002", "entity-003"),
    )

    response = await service(topology).find_semantic_paths(
        SemanticPathRequest(ACCESS, "entity-000", "entity-003", traversal="directed", limit=5)
    )

    assert sorted([item.assertion_id for item in path] for path in response.paths) == [
        ["entity-000->entity-001", "entity-001->entity-003"],
        ["entity-000->entity-002", "entity-002->entity-003"],
    ]
    assert response.truncated is False
