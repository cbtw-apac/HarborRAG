from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.chunking import ChunkRecord, RelationType
from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.ingestion import GraphNodeRecord, KnowledgeNodeKind

from .graph_models import (
    GraphDocumentTarget,
    GraphProjectionBatch,
    GraphProjectionInput,
    UnresolvedGraphRelation,
)
from .graph_state import (
    GraphNodeSpec,
    GraphProjectionContext,
    GraphProjectionState,
    GraphRelationSpec,
)
from .graph_structure import StructuralGraphProjector, reviewable_preview


class GraphProjectionBuilder:
    """Build the non-LLM structural and source-explicit knowledge graph."""

    def build_structural(
        self,
        *,
        document: Document,
        chunks: tuple[ChunkRecord, ...],
        graph_projection_version: str,
    ) -> GraphProjectionBatch:
        """Build the immutable per-version graph before link-target repair."""

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
        evidence_chunks = (
            tuple(chunk for chunk in request.chunks if chunk.record_kind.value == "evidence")
            or request.chunks
        )
        current_document = state.current_node(
            KnowledgeNodeKind.DOCUMENT,
            context.document_id,
            title=request.document.title,
            content_preview=reviewable_preview(evidence_chunks),
        )
        StructuralGraphProjector(state, request.chunks).project(current_document)
        unresolved = SourceRelationProjector(
            state=state,
            current_document=current_document,
            resolved_targets=request.resolved_targets,
        ).project(request.document.relations)
        return GraphProjectionBatch(
            nodes=tuple(state.nodes[key] for key in sorted(state.nodes)),
            relations=tuple(state.relations[key] for key in sorted(state.relations)),
            unresolved_relations=unresolved,
        )


class SourceRelationProjector:
    """Normalize source relations and defer targets that are not yet published."""

    def __init__(
        self,
        *,
        state: GraphProjectionState,
        current_document: GraphNodeRecord,
        resolved_targets: Mapping[str, GraphDocumentTarget],
    ) -> None:
        self._state = state
        self._current = current_document
        self._targets = resolved_targets

    def project(
        self,
        relations: list[DocumentRelation],
    ) -> tuple[UnresolvedGraphRelation, ...]:
        unresolved: list[UnresolvedGraphRelation] = []
        seen: set[tuple[RelationType, str, str]] = set()
        for relation in relations:
            normalized = self._normalize(relation)
            if normalized is None:
                continue
            relation_type, reverse = normalized
            target = self._targets.get(relation.target_id)
            if target is None:
                unresolved.append(
                    UnresolvedGraphRelation(
                        relation_type=relation_type.value,
                        target_source_item_id=relation.target_id,
                    )
                )
                continue
            target_node = self._state.node(
                GraphNodeSpec(
                    kind=KnowledgeNodeKind.DOCUMENT,
                    logical_id=target.document_id,
                    document_id=target.document_id,
                    document_version_id=target.document_version_id,
                    source_scope_id=target.source_scope_id,
                    title=target.title or target.source_item_id,
                    source_item_id=target.source_item_id,
                )
            )
            source_node, destination_node = (
                (target_node, self._current) if reverse else (self._current, target_node)
            )
            key = (relation_type, source_node.node_key, destination_node.node_key)
            if key in seen:
                continue
            seen.add(key)
            supplied_version = relation.metadata.get("source_relation_version")
            self._state.relation(
                GraphRelationSpec(
                    relation_type=relation_type,
                    source=source_node,
                    target=destination_node,
                    source_explicit=True,
                    source_relation_version=(
                        str(supplied_version)
                        if supplied_version is not None and str(supplied_version).strip()
                        else None
                    ),
                )
            )
        return tuple(unresolved)

    @staticmethod
    def _normalize(
        relation: DocumentRelation,
    ) -> tuple[RelationType, bool] | None:
        predicate = relation.predicate.strip().lower().replace("-", "_").replace(" ", "_")
        registry: dict[str, tuple[RelationType, bool]] = {
            "has_attachment": (RelationType.HAS_ATTACHMENT, False),
            "attached_to": (RelationType.HAS_ATTACHMENT, True),
            "child_of": (RelationType.CHILD_OF, False),
            "parent_of": (RelationType.CHILD_OF, True),
            "links_to": (RelationType.LINKS_TO, False),
            "includes": (RelationType.INCLUDES, False),
            "embeds": (RelationType.EMBEDS, False),
            "blocks": (RelationType.BLOCKS, False),
            "is_blocked_by": (RelationType.BLOCKS, True),
            "duplicates": (RelationType.DUPLICATES, False),
            "is_duplicated_by": (RelationType.DUPLICATES, True),
            "relates_to": (RelationType.RELATES_TO, False),
        }
        return registry.get(predicate)
