from __future__ import annotations

from harborrag_core.chunking import ChunkRecord, RelationType
from harborrag_core.domain.document import Document

from .graph_models import GraphProjectionBatch, GraphProjectionInput
from .graph_state import GraphProjectionContext, GraphProjectionState, GraphRelationSpec
from .graph_structure import StructuralGraphProjector
from .source_projectors import (
    GraphSourceProjectorRegistry,
    default_graph_source_projector_registry,
)
from .source_relations import SourceRelationProjector


class GraphProjectionBuilder:
    """Build schema-v2 tenant, source, version, structure, and chunk topology."""

    def __init__(self, registry: GraphSourceProjectorRegistry | None = None) -> None:
        self._registry = registry or default_graph_source_projector_registry()

    def build_structural(
        self,
        *,
        document: Document,
        chunks: tuple[ChunkRecord, ...],
        graph_projection_version: str,
    ) -> GraphProjectionBatch:
        return self.build(
            GraphProjectionInput(
                document=document,
                chunks=chunks,
                resolved_targets={},
                graph_projection_version=graph_projection_version,
            )
        )

    def build(self, request: GraphProjectionInput) -> GraphProjectionBatch:
        first = request.chunks[0]
        context = GraphProjectionContext(
            tenant_id=first.tenant_id,
            connection_id=first.connection_id,
            document_id=first.document_id,
            document_version_id=first.document_version_id,
            source_scope_id=first.source_scope_id,
            source_relation_version=request.graph_projection_version,
            connector_type=first.connector_type,
            document_kind=first.document_kind,
            source_item_id=first.source_item_id,
            source_uri=first.citation_locator.uri,
        )
        state = GraphProjectionState(context)
        tenant = state.tenant_node()
        data_source = state.data_source_node()
        state.relation(
            GraphRelationSpec(
                relation_type=RelationType.HAS_DATA_SOURCE,
                source=tenant,
                target=data_source,
                source_explicit=False,
            )
        )
        document_version = state.document_version_node(title=request.document.title)
        source_item = self._registry.resolve(first.connector_type.value).project(
            state,
            request.document,
            data_source,
            document_version,
        )
        StructuralGraphProjector(state, request.chunks).project(document_version)
        unresolved = SourceRelationProjector(
            state=state,
            current_source_item=source_item,
            resolved_targets=request.resolved_targets,
            external_stubs=request.external_stubs,
        ).project(request.document.relations)
        return GraphProjectionBatch(
            nodes=tuple(state.nodes.values()),
            relations=tuple(state.relations.values()),
            unresolved_relations=unresolved,
        )


__all__ = ["GraphProjectionBuilder", "SourceRelationProjector"]
