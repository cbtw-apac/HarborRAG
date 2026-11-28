"""Past the document enumeration budget, the graph ACL admits documents by readable source."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (
    access_parameters,
    access_predicate,
)
from harborrag_core.chunking import RelationType
from harborrag_core.retrieval import GraphAccessScope, GraphTripletQuery
from harborrag_core.retrieval.graph import GraphDocumentAccess
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem

pytestmark = pytest.mark.unit

BY_SOURCE = GraphAccessScope(
    source_scope_ids=("scope-engineering",),
    document_access=GraphDocumentAccess.BY_SOURCE_SCOPE,
)


def test_a_listed_scope_keeps_documents_to_their_ids() -> None:
    parameters = access_parameters(GraphAccessScope(document_ids=("document-1",)))

    assert parameters["documents_by_source_scope"] is False
    assert parameters["authorized_document_ids"] == ["document-1"]
    assert access_parameters(None)["documents_by_source_scope"] is False


def test_a_source_scope_fallback_admits_documents_of_readable_sources() -> None:
    parameters = access_parameters(BY_SOURCE)
    predicate = " ".join(access_predicate("node").split())

    assert parameters["documents_by_source_scope"] is True
    assert parameters["authorized_document_ids"] == []
    assert parameters["authorized_source_scope_ids"] == ["scope-engineering"]
    assert parameters["access_unrestricted"] is False
    assert (
        "$documents_by_source_scope AND node.document_id IS NOT NULL "
        "AND node.source_scope_id IN $authorized_source_scope_ids"
    ) in predicate


def test_a_source_scope_fallback_cannot_also_list_documents() -> None:
    with pytest.raises(ValueError, match="must not list document_ids"):
        GraphAccessScope(
            document_ids=("document-1",),
            source_scope_ids=("scope-engineering",),
            document_access=GraphDocumentAccess.BY_SOURCE_SCOPE,
        )


@pytest.mark.asyncio
async def test_the_fallback_is_applied_before_the_read_is_limited_and_never_inlined() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [
        FakeQueryResult([HeaderItem("subject"), HeaderItem("predicate"), HeaderItem("object")], [])
    ]
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )

    await repository.search_triplets(
        GraphTripletQuery(predicate=RelationType.CONTAINS, access_scope=BY_SOURCE),
        context=StorageOperationContext.system("tenant-1"),
    )

    statement, parameters = client.read_calls[0]
    assert parameters["documents_by_source_scope"] is True
    assert "scope-engineering" not in statement
    assert statement.index("$documents_by_source_scope") < statement.index("LIMIT $scan_limit")
