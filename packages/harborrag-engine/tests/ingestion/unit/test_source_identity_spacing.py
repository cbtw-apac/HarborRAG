"""Regression: graph keys and reindex keep the edge spaces of a source item's path."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType, DocumentKind
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType, identity_for_source
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput
from harborrag_engine.ingestion.maintenance.reindex import source_identity_from_canonical

from .chunking_helpers import make_document, make_profile, make_request, make_service

pytestmark = pytest.mark.unit


def _file_node_key(connector: str, extra: dict[str, object], source_item_id: str) -> str:
    document = make_document(
        [DocumentElement("p1", "paragraph", "Release checklist")],
        source=connector,
        extra=extra,
    )
    chunks = tuple(
        chunk.model_copy(
            update={
                "connector_type": ConnectorType(connector),
                "document_kind": DocumentKind(f"{connector}_file"),
                "source_item_id": source_item_id,
            }
        )
        for chunk in make_service(make_profile(target=40, maximum=60), configuration_version="3")
        .chunk(make_request(make_document(document.content)))
        .chunks
    )
    graph = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="graph-v2",
        )
    )
    entity_type = {
        "local": GraphEntityType.LOCAL_FILE,
        "github": GraphEntityType.GITHUB_FILE,
    }[connector]
    return next(node.node_key for node in graph.nodes if node.entity_type == entity_type)


def test_local_files_whose_paths_differ_only_in_edge_spaces_get_two_file_nodes() -> None:
    keys = {
        _file_node_key("local", {"relative_path": path}, path)
        for path in ("notes.md", " notes.md", "notes.md ")
    }

    assert len(keys) == 3


def test_github_files_whose_paths_differ_only_in_edge_spaces_get_two_file_nodes() -> None:
    keys = {
        _file_node_key(
            "github", {"path": path, "repo": "handbook"}, f"github://demo/handbook/{path}"
        )
        for path in ("docs/notes.md", "docs/notes.md ")
    }

    assert len(keys) == 2


def test_reindex_recovers_the_stored_identity_of_a_path_with_an_edge_space() -> None:
    source_item_id = "github://demo/handbook/docs/notes.md "
    canonical = make_document(
        [DocumentElement("p1", "paragraph", "Release checklist")],
        extra={
            "tenant_id": "acme",
            "connector_type": "github",
            "connection_id": "handbook",
            "source_item_id": source_item_id,
            "source_scope_id": "scope-handbook",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": " docs/index.md",
        },
    )

    identity = source_identity_from_canonical(canonical)

    assert identity.source_item_id == source_item_id
    assert identity.binding.parent_source_item_id == " docs/index.md"
    assert identity_for_source(identity) != identity_for_source(
        identity.model_copy(update={"source_item_id": source_item_id.strip()})
    )
