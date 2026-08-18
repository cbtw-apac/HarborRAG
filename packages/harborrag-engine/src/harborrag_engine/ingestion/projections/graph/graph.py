from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.chunking import ChunkRecord, RelationType
from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.ingestion import GraphNodeRecord

from .graph_models import (
    GraphDocumentTarget,
    GraphProjectionBatch,
    GraphProjectionInput,
    UnresolvedGraphRelation,
)
from .graph_state import GraphProjectionContext, GraphProjectionState, GraphRelationSpec
from .graph_structure import StructuralGraphProjector
from .source_projectors import (
    GraphSourceProjectorRegistry,
    default_graph_source_projector_registry,
    relation_entity_type,
    source_provider_id,
)


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
        ).project(request.document.relations)
        return GraphProjectionBatch(
            nodes=tuple(state.nodes.values()),
            relations=tuple(state.relations.values()),
            unresolved_relations=unresolved,
        )


class SourceRelationProjector:
    """Project source-native links between stable source entities."""

    def __init__(
        self,
        *,
        state: GraphProjectionState,
        current_source_item: GraphNodeRecord,
        resolved_targets: Mapping[str, GraphDocumentTarget],
    ) -> None:
        self._state = state
        self._current = current_source_item
        self._targets = resolved_targets

    def project(self, relations: list[DocumentRelation]) -> tuple[UnresolvedGraphRelation, ...]:
        unresolved: list[UnresolvedGraphRelation] = []
        seen: set[tuple[RelationType, str, str]] = set()
        for relation in relations:
            normalized = self._normalize(relation)
            if normalized is None:
                continue
            relation_type, reverse = normalized
            resolved = self._targets.get(relation.target_id)
            if resolved is None:
                unresolved.append(
                    UnresolvedGraphRelation(
                        relation_type=relation_type.value,
                        target_source_item_id=relation.target_id,
                    )
                )
            raw_target_id = resolved.source_item_id if resolved is not None else relation.target_id
            target_id = source_provider_id(
                self._state.context.connector_type.value,
                raw_target_id,
            )
            target_scope = (
                resolved.source_scope_id
                if resolved is not None
                else self._state.context.source_scope_id
            )
            # Every node this projector creates stands in for something another document
            # owns, resolved or not: it carries no provider attributes of its own, so it
            # must never overwrite the concrete projection (adapter writes placeholders
            # ON CREATE SET only). A resolved target only supplies a better stub title.
            target = self._state.source_node(
                relation_entity_type(
                    self._state.context.connector_type.value,
                    relation_type,
                    relation.target_type,
                ),
                target_id,
                title=((resolved.title or target_id) if resolved is not None else target_id),
                source_scope_id=target_scope,
                attributes={"placeholder": True},
            )
            source, destination = (target, self._current) if reverse else (self._current, target)
            key = (relation_type, source.node_key, destination.node_key)
            if key in seen:
                continue
            seen.add(key)
            supplied_version = relation.metadata.get("source_relation_version")
            self._state.relation(
                GraphRelationSpec(
                    relation_type=relation_type,
                    source=source,
                    target=destination,
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
    def _normalize(relation: DocumentRelation) -> tuple[RelationType, bool] | None:
        predicate = relation.predicate.strip().lower().replace("-", "_").replace(" ", "_")
        registry: dict[str, tuple[RelationType, bool]] = {
            "has_attachment": (RelationType.HAS_ATTACHMENT, False),
            "attached_to": (RelationType.HAS_ATTACHMENT, True),
            "child_of": (RelationType.PARENT_OF, True),
            "parent_of": (RelationType.PARENT_OF, False),
            "links_to": (RelationType.LINKS_TO, False),
            "blocks": (RelationType.BLOCKS, False),
            "is_blocked_by": (RelationType.BLOCKS, True),
            "duplicates": (RelationType.DUPLICATES, False),
            "is_duplicated_by": (RelationType.DUPLICATES, True),
            "relates_to": (RelationType.RELATES_TO, False),
        }
        return registry.get(predicate)
