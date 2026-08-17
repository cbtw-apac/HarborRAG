"""Lock the (source kind, relation, target kind) vocabulary of the graph projection."""

from __future__ import annotations

from typing import Any

from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion import (
    GraphDocumentTarget,
    GraphProjectionBatch,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service

# Reviewed edge-shape vocabulary of graph schema v2. A signature observed but absent
# here is a schema change: trace it to its builder code path, review, then add it.
# KEEP IN SYNC (by review) with CORPUS_SIGNATURES in
# packages/harborrag-runtime/tests/graph_eval/corpus.py -- test modules are not
# importable across packages, so that frozenset is a reviewed copy of this one. This
# test locks what the projection *can* emit; the eval corpus asserts equality with what
# it *does* emit, so the two have to stay identical set-for-set.
EXPECTED_SIGNATURES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("Tenant", "has_data_source", "DataSource"),
        ("DataSource", "contains", "SourceEntity"),
        ("SourceEntity", "has_version", "DocumentVersion"),
        ("DocumentVersion", "contains", "Structure"),
        ("Structure", "parent_of", "Structure"),
        ("Structure", "contains", "Structure"),
        ("Structure", "links_to", "Structure"),
        ("Structure", "reply_to", "Structure"),
        ("Chunk", "supports", "Structure"),
        ("Chunk", "supports", "DocumentVersion"),
        ("SourceEntity", "links_to", "SourceEntity"),
        ("SourceEntity", "parent_of", "SourceEntity"),
        ("SourceEntity", "blocks", "SourceEntity"),
        ("SourceEntity", "duplicates", "SourceEntity"),
        ("SourceEntity", "relates_to", "SourceEntity"),
        ("SourceEntity", "has_attachment", "SourceEntity"),
        ("SourceEntity", "contains", "SourceEntity"),
        ("SourceEntity", "points_to", "SourceEntity"),
        ("DocumentVersion", "resolved_at", "SourceEntity"),
    }
)

# The signatures the fixtures must keep producing; if these go missing the fixtures
# have rotted and the superset assertion below is no longer meaningful. The first five
# are the structural spine every connector shares.
REQUIRED_SIGNATURES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("Tenant", "has_data_source", "DataSource"),
        ("SourceEntity", "has_version", "DocumentVersion"),
        ("DocumentVersion", "contains", "Structure"),
        ("Chunk", "supports", "Structure"),
        ("SourceEntity", "links_to", "SourceEntity"),
        # Provider-specific, single-source: github is the only connector projecting a ref
        # to a commit, so nothing else would notice if its provenance fixture rotted.
        ("SourceEntity", "points_to", "SourceEntity"),
        ("DocumentVersion", "resolved_at", "SourceEntity"),
    }
)

# Connector keys as `default_graph_source_projector_registry` registers them and as the
# runtime passes them (`source_identity.connector_type.value`) — "local", not "local_file".
# `CanonicalChunkFactory` reads provenance.extra["connector_type"] ahead of the request
# field, so seeding it here is what stamps ChunkRecord.connector_type for the builder.
# Each entry carries the provider metadata its projector needs for a full hierarchy:
# github needs ref + commit_sha for points_to/resolved_at, sharepoint needs a parent path
# two folders deep for nested SharePoint folders.
SOURCE_PROVENANCE: dict[str, dict[str, Any]] = {
    "local": {"relative_path": "docs/runbooks/guide.md"},
    "confluence": {
        "space_id": "space-1",
        "space_key": "ENG",
        "page_id": "page-2",
        "ancestor_ids": ["page-1"],
        "ancestor_titles": ["Parent"],
    },
    "jira": {
        "project_id": "project-1",
        "project_key": "ENG",
        "issue_key": "ENG-2",
        "parent": {"key": "ENG-1", "summary": "Parent issue"},
    },
    "github": {
        "owner": "acme",
        "repo": "harbor",
        "repository_id": "repo-1",
        "path": "docs/guide.md",
        "ref": "main",
        "commit_sha": "abcdef1234567890",
    },
    "sharepoint": {
        "site_id": "site-1",
        "drive_id": "drive-1",
        "item_id": "file-1",
        "parent": {"id": "folder-2", "path": "/drives/drive-1/root:/Policies/Security"},
    },
}


def _signatures(batch: GraphProjectionBatch) -> set[tuple[str, str, str]]:
    kinds = {node.node_key: node.node_kind.value for node in batch.nodes}
    return {
        (
            kinds[relation.source_node_key],
            relation.relation_type.value,
            kinds[relation.target_node_key],
        )
        for relation in batch.relations
    }


def _batch_for(source: str) -> GraphProjectionBatch:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Operations", {"level": 1}),
            DocumentElement("h2", "heading", "Runbooks", {"level": 2}),
            DocumentElement("p1", "paragraph", "Run the worker."),
            DocumentElement("table-1", "table", "Mode\tTimeout\nprod\t30\n"),
        ],
        source=source,
        extra={"connector_type": source, **SOURCE_PROVENANCE[source]},
    )
    document.relations = [
        DocumentRelation(predicate="links_to", target_id="target-page", target_type="document"),
        DocumentRelation(predicate="child_of", target_id="target-page", target_type="document"),
        DocumentRelation(predicate="blocks", target_id="target-page", target_type="document"),
        DocumentRelation(predicate="duplicates", target_id="target-page", target_type="document"),
        DocumentRelation(predicate="relates_to", target_id="target-page", target_type="document"),
        DocumentRelation(
            predicate="has_attachment", target_id="attachment-1", target_type="attachment"
        ),
        DocumentRelation(predicate="links_to", target_id="never-published", target_type="document"),
    ]
    chunks = (
        make_service(make_profile(target=40, maximum=60), create_route_chunks=True)
        .chunk(make_request(document))
        .chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={
                "target-page": GraphDocumentTarget(
                    source_item_id="target-page",
                    document_id="document-target",
                    document_version_id="document-target-version",
                    source_scope_id="tenant-1",
                    title=None,
                )
            },
            graph_projection_version="graph-v1",
        )
    )


def test_projection_signatures_stay_within_reviewed_vocabulary() -> None:
    observed: set[tuple[str, str, str]] = set()
    for source in SOURCE_PROVENANCE:
        observed |= _signatures(_batch_for(source))
    unexpected = observed - EXPECTED_SIGNATURES
    assert not unexpected, f"unreviewed projection signatures: {sorted(unexpected)}"
    missing = REQUIRED_SIGNATURES - observed
    assert not missing, f"fixtures no longer exercise the spine: {sorted(missing)}"
