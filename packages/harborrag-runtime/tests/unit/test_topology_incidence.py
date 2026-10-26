"""Graph discovery must work without assertions and stay permission/query bounded."""

from __future__ import annotations

import pytest
from topology_retrieval_support import CanonicalTopology, mention

from harborrag_core.security import AccessContext
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_engine.topology.retrieval import LocalTopologySearch

ACCESS = AccessContext.system("tenant-1")


@pytest.mark.asyncio
async def test_incidence_discovers_passage_without_any_typed_assertion():
    repository = CanonicalTopology()
    repository.assertions = ()
    repository.mentions = (
        mention("shared", "chunk-1", "Alpha"),
        mention("shared", "chunk-2", "Alpha", owner=("build-2", "document-2", "version-2")),
    )
    expanded = await LocalTopologySearch(repository).expand(
        "Alpha details", ("chunk-1",), tenant_id="tenant-1", access=ACCESS
    )
    assert [item.chunk_id for item in expanded.evidence] == ["chunk-1", "chunk-2"]
    bridge = expanded.evidence[1]
    assert bridge.assertion_ids == ()
    assert bridge.build_ids == ("build-1", "build-2")
    assert bridge.paths[0].kind == "mention_incidence"
    assert bridge.paths[0].entity_ids == ("shared",)


@pytest.mark.asyncio
async def test_untrusted_or_missing_access_never_starts_entity_lookup():
    repository = CanonicalTopology()
    for access in (None, AccessContext.system("other")):
        result = await LocalTopologySearch(repository).expand(
            "Alpha", ("chunk-1",), tenant_id="tenant-1", access=access
        )
        assert not result.evidence
    assert not repository.calls


@pytest.mark.asyncio
async def test_same_name_in_seed_is_not_an_authoritative_cross_document_identity():
    repository = CanonicalTopology()
    repository.assertions = ()
    repository.mentions = (
        mention("left", "chunk-1", "Alpha"),
        mention("right", "chunk-2", "Alpha"),
    )
    expanded = await LocalTopologySearch(repository).expand(
        "details", ("chunk-1",), tenant_id="tenant-1", access=ACCESS
    )
    assert [item.chunk_id for item in expanded.evidence] == ["chunk-1"]


@pytest.mark.asyncio
async def test_typed_paths_stop_at_two_hops_and_keep_original_direction_and_qualifiers():
    repository = CanonicalTopology()
    prototype = repository.assertions[0]
    repository.assertions = tuple(
        prototype.model_copy(
            update={
                "assertion_id": f"a-{i}",
                "subject_entity_id": f"e-{i}",
                "object_entity_id": f"e-{i + 1}",
                "chunk_id": f"chunk-{i}",
                "observation": prototype.observation.model_copy(
                    update={
                        "modality": "possible",
                        "polarity": "negative",
                        "time_qualifier": "next quarter",
                    }
                ),
            }
        )
        for i in range(1, 4)
    )
    repository.mentions = tuple(mention(f"e-{i}", f"chunk-{i}", "Alpha") for i in range(1, 5))
    expanded = await LocalTopologySearch(repository).expand(
        "dependencies", ("chunk-1",), tenant_id="tenant-1", access=ACCESS
    )
    assert {item.chunk_id for item in expanded.evidence} == {"chunk-1", "chunk-2", "chunk-3"}
    target = next(item for item in expanded.evidence if item.chunk_id == "chunk-3")
    assert target.paths[0].assertion_ids == ("a-1", "a-2")
    assert target.assertions[-1].subject_entity_id == "e-2"
    assert target.assertions[-1].observation.modality == "possible"
    assert target.assertions[-1].observation.polarity == "negative"


@pytest.mark.asyncio
async def test_query_relation_matching_prunes_incompatible_typed_edges_but_keeps_incidence():
    repository = CanonicalTopology()
    expanded = await LocalTopologySearch(repository).expand(
        "who owns Alpha", ("chunk-1",), tenant_id="tenant-1", access=ACCESS
    )
    assert [item.chunk_id for item in expanded.evidence] == ["chunk-1"]
    assert expanded.assertions == 0


@pytest.mark.asyncio
async def test_authorized_high_degree_entity_is_suppressed_instead_of_arbitrary_prefix():
    repository = CanonicalTopology()
    repository.assertions = ()
    repository.mentions = tuple(mention("generic", f"chunk-{i}", "System") for i in range(102))
    expanded = await LocalTopologySearch(repository).expand(
        "details", ("chunk-0",), tenant_id="tenant-1", access=ACCESS
    )
    assert [item.chunk_id for item in expanded.evidence] == ["chunk-0"]
    assert expanded.suppressed_entities == 1
    assert expanded.truncated


@pytest.mark.asyncio
async def test_seed_and_candidate_limits_are_configurable_downwards():
    repository = CanonicalTopology()
    repository.assertions = ()
    repository.mentions = tuple(mention("shared", f"chunk-{i}", "Alpha") for i in range(10))
    policy = TopologyRetrievalPolicy(seed_chunks=1, max_entities=1, max_candidate_chunks=3)
    expanded = await LocalTopologySearch(repository, policy).expand(
        "details", tuple(f"chunk-{i}" for i in range(10)), tenant_id="tenant-1", access=ACCESS
    )
    assert len(expanded.evidence) == 3
    assert expanded.truncated
    assert repository.calls[0][2] == ("chunk-0",)


@pytest.mark.asyncio
async def test_every_canonical_lookup_uses_original_principal():
    class Guarded(CanonicalTopology):
        async def active_mentions(self, tenant_id, *, access=None, **selectors):
            assert access == ACCESS
            return await super().active_mentions(tenant_id, access=access, **selectors)

        async def active_assertions(self, tenant_id, *, access=None, **selectors):
            assert access == ACCESS
            return await super().active_assertions(tenant_id, access=access, **selectors)

    expanded = await LocalTopologySearch(Guarded()).expand(
        "dependencies", ("chunk-1",), tenant_id="tenant-1", access=ACCESS
    )
    assert len(expanded.evidence) == 2
