"""Two-document projection helpers shared by the node-key convergence suites.

A source-entity node key hashes tenant, scope, entity type and provider id, so a
placeholder and the concrete node it stands in for converge only when all four
agree -- and a mismatch is silent, because ``GraphProjectionState`` only detects
conflicts within a single build. Every suite that uses these therefore projects
two documents and compares their keys.
"""

from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.chunking import ConnectorType, DocumentKind
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType
from harborrag_engine.ingestion import (
    GraphDocumentTarget,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service

__all__ = ["keys", "project"]


def project(  # noqa: PLR0913
    connector: str,
    extra: dict[str, object],
    *,
    source_item_id: str,
    relations: list[DocumentRelation] | None = None,
    resolved_targets: Mapping[str, GraphDocumentTarget] | None = None,
    external_stubs: bool = False,
    source_scope_id: str | None = None,
):
    document = make_document(
        [DocumentElement("p1", "paragraph", "Provider evidence")],
        source=connector,
        extra=extra,
    )
    document.relations = relations or []
    chunks = (
        make_service(
            make_profile(target=40, maximum=60),
            configuration_version="3",
            create_route_chunks=True,
        )
        .chunk(make_request(make_document(document.content)))
        .chunks
    )
    rebound = tuple(
        chunk.model_copy(
            update={
                "connector_type": ConnectorType(connector),
                "document_kind": DocumentKind(f"{connector}_file"),
                "source_item_id": source_item_id,
                **({"source_scope_id": source_scope_id} if source_scope_id else {}),
            }
        )
        for chunk in chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=rebound,
            resolved_targets=resolved_targets or {},
            graph_projection_version="graph-v2",
            external_stubs=external_stubs,
        )
    )


def keys(graph, entity_type: GraphEntityType) -> dict[str, str]:
    return {
        node.logical_id: node.node_key for node in graph.nodes if node.entity_type is entity_type
    }
