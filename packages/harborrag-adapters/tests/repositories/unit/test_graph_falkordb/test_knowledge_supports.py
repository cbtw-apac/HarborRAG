"""Support reconciliation and shared metadata respect their publication boundary."""

from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.falkordb.knowledge_mapping import KnowledgeGraphMapper
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem
from .test_knowledge import nodes, relation, repository

CONTEXT = StorageOperationContext.system("tenant-1")


def verified_rows(client: FakeFalkorDBClient) -> None:
    client.read_results = [
        FakeQueryResult(
            [HeaderItem("node_key"), HeaderItem("occurrences")],
            [[node.node_key, 1] for node in nodes()],
        ),
        FakeQueryResult(
            [
                HeaderItem("relation_id"),
                HeaderItem("source_node_key"),
                HeaderItem("target_node_key"),
                HeaderItem("occurrences"),
            ],
            [["relation-1", "node-document", "node-section", 1]],
        ),
    ]


@pytest.mark.asyncio
async def test_shared_source_rows_contain_identity_not_unpublished_provider_metadata() -> None:
    client = FakeFalkorDBClient()
    node = GraphNodeRecord(
        node_key="source-key",
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.JIRA_ISSUE,
        logical_id="ISSUE-1",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        title="Unpublished change",
        attributes={"status": "new status"},
    )

    await repository(client).upsert_nodes((node,), context=CONTEXT)

    stored = client.write_calls[0][1]["rows"][0]
    assert stored["title"] == "ISSUE-1"
    assert FalkorDBMapper.decode_property(stored["attributes"]) == {}


@pytest.mark.asyncio
async def test_replacement_verifies_before_retracting_only_named_version_links() -> None:
    client = FakeFalkorDBClient()
    verified_rows(client)
    support = relation().model_copy(update={"attributes": {"source_relation": True}})

    await repository(client).replace_source_relations(
        "version-1", nodes(), (support,), context=CONTEXT
    )

    statement, parameters = client.write_calls[-1]
    assert "DELETE relation" in statement
    assert "relation.ownership_scope = 'DOCUMENT_VERSION'" in statement
    assert "relation.source_relation = true" in statement
    assert parameters["tenant_id"] == "tenant-1"
    assert parameters["document_version_id"] == "version-1"
    assert parameters["retained_ids"] == ["relation-1"]
    assert len(client.read_calls) == 2


@pytest.mark.asyncio
async def test_failed_replacement_verification_preserves_previous_links() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [FakeQueryResult([], []), FakeQueryResult([], [])]
    support = relation().model_copy(update={"attributes": {"source_relation": True}})

    with pytest.raises(ValueError, match="failed verification"):
        await repository(client).replace_source_relations(
            "version-1", nodes(), (support,), context=CONTEXT
        )

    assert not any("DELETE relation" in statement for statement, _ in client.write_calls)


@pytest.mark.asyncio
async def test_empty_replacement_retracts_all_previously_resolved_links() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [FakeQueryResult([], []), FakeQueryResult([], [])]

    await repository(client).replace_source_relations("version-1", (), (), context=CONTEXT)

    assert len(client.write_calls) == 1
    assert client.write_calls[0][1]["retained_ids"] == []


@pytest.mark.asyncio
async def test_replacement_rejects_foreign_version_before_any_write() -> None:
    client = FakeFalkorDBClient()
    support = relation().model_copy(update={"attributes": {"source_relation": True}})

    with pytest.raises(ValueError, match="named version"):
        await repository(client).replace_source_relations(
            "different-version", nodes(), (support,), context=CONTEXT
        )

    assert not client.write_calls


@pytest.mark.asyncio
async def test_legacy_retirement_requires_verified_new_scope_manifests() -> None:
    client = FakeFalkorDBClient()
    verified_rows(client)

    await repository(client).retire_legacy_source_relations(
        "scope-1", nodes(), (relation(),), context=CONTEXT
    )

    assert len(client.write_calls) == 1
    statement, parameters = client.write_calls[0]
    assert "relation.ownership_scope = 'SOURCE_SCOPE'" in statement
    assert "relation.relation_type <> 'has_data_source'" in statement
    assert parameters["source_scope_id"] == "scope-1"
    assert parameters["tenant_id"] == "tenant-1"


@pytest.mark.asyncio
async def test_legacy_retirement_does_not_delete_when_manifests_are_missing() -> None:
    client = FakeFalkorDBClient()
    client.read_results = [FakeQueryResult([], []), FakeQueryResult([], [])]

    with pytest.raises(ValueError, match="failed verification"):
        await repository(client).retire_legacy_source_relations(
            "scope-1", nodes(), (relation(),), context=CONTEXT
        )

    assert not client.write_calls


def test_source_observation_selector_columns_are_not_public_contract_fields() -> None:
    edge = relation()
    mapped = KnowledgeGraphMapper.relation(
        {
            **edge.model_dump(),
            "source_title": "Source",
            "target_title": "Target",
            "source_relation": True,
        }
    )
    assert mapped == edge
