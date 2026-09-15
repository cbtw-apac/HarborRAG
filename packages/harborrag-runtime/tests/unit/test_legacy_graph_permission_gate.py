"""Graph dispatch remains fail-closed when no structural repository is configured."""

from dataclasses import replace

import pytest
from retrieval_test_support import policy, resources
from topology_retrieval_support import CanonicalTopology

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.retrieval import GraphTripletQuery
from harborrag_core.security import AccessContext
from harborrag_runtime.retrieval import RuntimeRetrievalService


@pytest.mark.asyncio
async def test_document_authority_does_not_invent_a_graph_backend():
    service = RuntimeRetrievalService(
        resources=replace(resources(), topology_repository=CanonicalTopology()),
        policy=policy(),
    )
    with pytest.raises(HarborCapabilityError, match="graph retrieval is not configured"):
        await service.search_graph_triplets(
            GraphTripletQuery(predicate=RelationType.LINKS_TO),
            access=AccessContext.system("tenant-1"),
        )
    assert service._graph_search is None


@pytest.mark.asyncio
async def test_missing_graph_backend_precedes_path_and_subgraph_dispatch():
    service = RuntimeRetrievalService(
        resources=replace(resources(), topology_repository=CanonicalTopology()),
        policy=policy(),
    )
    for method in (service.search_graph_paths, service.search_graph_subgraph):
        with pytest.raises(HarborCapabilityError, match="graph retrieval is not configured"):
            # No query is inspected or sent to a storage provider after the gate.
            await method(None, access=AccessContext.system("tenant-1"))
