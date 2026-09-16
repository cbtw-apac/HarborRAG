"""Parent summaries enrich structural nodes without duplicate wrapper nodes."""

import pytest

from harborrag_adapters.repositories.graph.falkordb.parent_views import ParentViewBuilder
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import ParentDescription

from .fakes import FakeFalkorDBClient
from .test_topology import CONTEXT, repository, result
from .test_typed_topology import readback, typed_build


def parents():
    return (
        ParentDescription(
            parent_key="doc-1",
            level="document",
            description="Qualified dependency overview.",
            input_chunk_ids=("chunk-1",),
            cited_chunk_ids=("chunk-1",),
            input_digest="current-inputs",
        ),
    )


@pytest.mark.asyncio
async def test_parent_summaries_write_directly_to_the_structural_target():
    value = typed_build()
    client = FakeFalkorDBClient()
    graph = repository(client)
    await graph.write_parents(value, parents(), context=CONTEXT)
    assert len(client.write_calls) == 2
    cleanup, cleanup_parameters = client.write_calls[0]
    assert "DETACH DELETE view" in cleanup
    assert cleanup_parameters == {"tenant_id": "tenant-1", "build_id": "build-1"}
    statement, parameters = client.write_calls[1]
    assert "MATCH (target:KnowledgeNode:DocumentVersion)" in statement
    assert "TopologyParentView" not in statement and "DESCRIBES" not in statement
    row = parameters["rows"][0]
    assert row["tenant_id"] == "tenant-1" and row["build_id"] == "build-1"
    assert row["document_version_id"] == "version-1"
    assert row["description"] == parents()[0].description
    assert row["input_digest"] == "current-inputs"
    client.read_results = [
        result(
            [
                {
                    "id": row["id"],
                    "expected_description": row["description"],
                    "descriptions": [row["description"]],
                    "occurrences": 1,
                }
            ]
        ),
    ]
    assert await graph.verify_parents(value, parents(), context=CONTEXT)
    assert "OPTIONAL MATCH (target:KnowledgeNode:DocumentVersion)" in client.read_calls[-1][0]
    # The primary typed manifest remains unchanged by the description overlay.
    nodes, edges = readback(value)
    client.read_results = [result(nodes), result(edges)]
    assert await graph.verify(value, context=CONTEXT)


@pytest.mark.asyncio
async def test_section_summary_targets_stable_structure_identity_not_repeated_label_path():
    value = typed_build()
    section = ParentDescription(
        parent_key="parent-section-a",
        level="section",
        section_path=("Status",),
        structure_id="source-section-a",
        description="Status for the first source section.",
        input_chunk_ids=("chunk-1",),
        cited_chunk_ids=("chunk-1",),
        input_digest="section-inputs",
    )
    client = FakeFalkorDBClient()
    await repository(client).write_parents(value, (section,), context=CONTEXT)

    statement, parameters = client.write_calls[1]
    assert "target.logical_id = row.structure_id" in statement
    assert "target.section_path = row.section_path" not in statement
    assert parameters["rows"][0]["structure_id"] == "source-section-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["text", "missing", "duplicate"])
async def test_parent_verification_rejects_stale_or_incomplete_projection(corruption):
    value = typed_build()
    canonical = ParentViewBuilder().build(value, parents(), "tenant-1")[0]
    rows = [
        {
            "id": canonical["id"],
            "expected_description": canonical["description"],
            "descriptions": [canonical["description"]],
            "occurrences": 1,
        }
    ]
    if corruption == "text":
        rows[0]["descriptions"] = ["Changed without regenerated artifact"]
    elif corruption == "missing":
        rows.clear()
    else:
        rows[0]["occurrences"] = 2
    client = FakeFalkorDBClient()
    client.read_results = [result(rows)]
    assert not await repository(client).verify_parents(value, parents(), context=CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"level": "folder"},
        {"input_chunk_ids": ("foreign",)},
        {"cited_chunk_ids": ("foreign",)},
        {"parent_key": ""},
    ],
)
async def test_incomplete_or_foreign_parent_is_rejected_before_write(changes):
    client = FakeFalkorDBClient()
    altered = (parents()[0].model_copy(update=changes),)
    with pytest.raises(ValueError):
        await repository(client).write_parents(typed_build(), altered, context=CONTEXT)
    assert not client.write_calls


@pytest.mark.asyncio
async def test_foreign_tenant_and_duplicate_parent_keys_are_rejected():
    client = FakeFalkorDBClient()
    with pytest.raises(ValueError, match="tenant"):
        await repository(client).write_parents(
            typed_build(), parents(), context=StorageOperationContext.system("tenant-2")
        )
    with pytest.raises(ValueError, match="duplicate"):
        await repository(client).write_parents(typed_build(), parents() * 2, context=CONTEXT)
    assert not client.write_calls


@pytest.mark.asyncio
async def test_delete_build_removes_only_named_build_primary_and_parent_views():
    client = FakeFalkorDBClient()
    await repository(client).delete_build("build-1", context=CONTEXT)
    assert len(client.write_calls) == 3
    chunk_statement, _ = client.write_calls[0]
    parent_statement, _ = client.write_calls[1]
    assert "n.description = 'Document evidence chunk.'" in chunk_statement
    assert "Document Version in the source topology." in parent_statement
    statement, parameters = client.write_calls[-1]
    assert "tenant_id: $tenant_id, build_id: $build_id" in statement
    assert "n:TopologyRecord OR n:TopologyParentView" in statement
    assert parameters == {"tenant_id": "tenant-1", "build_id": "build-1"}
