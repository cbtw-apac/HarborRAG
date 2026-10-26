"""An external stub's visibility must not depend on which declarer was projected last."""

from __future__ import annotations

import pytest
from test_graph_acl_retrieval import ScopedAuthorizer

from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval.graph_visibility import apply_graph_permissions

_STUB_KEY = DocumentIdentityBuilder().external_source_entity_node_key(
    tenant_id="tenant-1",
    connector_type="jira",
    connection_id="jira-main",
    source_item_id="jira://EXT/EXT-1",
)


def _stub(last_writer_scope: str, *, read_back: bool) -> GraphNodeRecord:
    """The one stub scopes A and B share, stamped with whichever projected last.

    Read back from the graph store, a shared source entity has lost its attributes,
    ``external`` included, so the rule must not depend on them.
    """

    return GraphNodeRecord(
        node_key=_STUB_KEY,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="EXT-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id=last_writer_scope,
        title="EXT-1",
        attributes={} if read_back else {"placeholder": True, "external": True},
    )


async def _state(record: GraphNodeRecord, authorizer: ScopedAuthorizer) -> str:
    states = await apply_graph_permissions(
        {"stub": record},
        {"stub": "active"},
        authorizer,
        StorageOperationContext.system("tenant-1"),
    )
    return states["stub"]


@pytest.mark.asyncio
@pytest.mark.parametrize("read_back", [False, True])
async def test_a_reader_of_one_scope_sees_the_shared_stub_whoever_wrote_it_last(
    read_back: bool,
) -> None:
    reader_of_a = ScopedAuthorizer(sources={"scope-a"})
    reader_of_b = ScopedAuthorizer(sources={"scope-b"})

    for last_writer in ("scope-a", "scope-b"):
        stub = _stub(last_writer, read_back=read_back)
        assert await _state(stub, reader_of_a) == "active", last_writer
        assert await _state(stub, reader_of_b) == "active", last_writer


@pytest.mark.asyncio
async def test_a_reader_with_no_grant_in_the_tenant_never_sees_a_stub() -> None:
    assert await _state(_stub("scope-a", read_back=True), ScopedAuthorizer()) == "denied"


@pytest.mark.asyncio
async def test_an_ordinary_source_entity_is_still_checked_against_its_scope() -> None:
    ordinary = _stub("scope-a", read_back=False).model_copy(
        update={"node_key": "graph-v2-source-entity:ordinary", "attributes": {}}
    )

    assert await _state(ordinary, ScopedAuthorizer(sources={"scope-b"})) == "denied"
    assert await _state(ordinary, ScopedAuthorizer(sources={"scope-a"})) == "active"
