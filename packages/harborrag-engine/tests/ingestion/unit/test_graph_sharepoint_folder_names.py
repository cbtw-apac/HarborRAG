"""Regression: SharePoint folder nodes carry real folder names, not Graph's encoding."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType, DocumentKind
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType
from harborrag_engine.ingestion import (
    GraphProjectionBatch,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service

pytestmark = pytest.mark.unit

SOURCE_ITEM_ID = "sharepoint://site-1/drive-1/file-1"


def _project_file_under(parent_path: str) -> GraphProjectionBatch:
    """Project one SharePoint file whose Graph parent reference has this path."""
    document = make_document(
        [DocumentElement("p1", "paragraph", "Provider evidence")],
        source="sharepoint",
        extra={
            "site_id": "site-1",
            "drive_id": "drive-1",
            "item_id": "file-1",
            "parent": {"id": "folder-2", "path": parent_path},
        },
    )
    chunks = tuple(
        chunk.model_copy(
            update={
                "connector_type": ConnectorType.SHAREPOINT,
                "document_kind": DocumentKind("sharepoint_file"),
                "source_item_id": SOURCE_ITEM_ID,
            }
        )
        for chunk in make_service(make_profile(target=40, maximum=60), configuration_version="3")
        .chunk(make_request(make_document(document.content)))
        .chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="graph-v2",
        )
    )


@pytest.mark.parametrize(
    ("parent_path", "folder_paths"),
    [
        (
            "/drives/drive-1/root:/Q3%20Plans/Design%C2%A0reviews",
            ["Q3 Plans", "Q3 Plans/Design\u00a0reviews"],
        ),
        ("/drives/drive-1/root:/Q3 Plans", ["Q3 Plans"]),
        ("/drives/drive-1/root:/T%C3%A0i%20li%E1%BB%87u", ["Tài liệu"]),
        ("/drives/drive-1/root:/Budget%252026", ["Budget%2026"]),
    ],
)
def test_sharepoint_folder_nodes_use_decoded_folder_names(
    parent_path: str, folder_paths: list[str]
) -> None:
    """Folder titles and paths are the names users see, decoded once from Graph."""
    graph = _project_file_under(parent_path)

    folders = [
        node for node in graph.nodes if node.entity_type == GraphEntityType.SHAREPOINT_FOLDER
    ]

    assert sorted(node.attributes["relative_path"] for node in folders) == folder_paths
    assert sorted(node.title for node in folders) == sorted(
        path.rsplit("/", 1)[-1] for path in folder_paths
    )
