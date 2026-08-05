from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from harborrag_core.chunking import ChunkKind, ChunkRecord, RecordKind, RelationType
from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.invariants import HarborInvariantError

from .graph_state import GraphNodeSpec, GraphProjectionState, GraphRelationSpec


class StructuralGraphProjector:
    """Project sections, tables, and comments contained by one document."""

    def __init__(
        self,
        state: GraphProjectionState,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        self._state = state
        self._chunks = tuple(chunks)

    def project(self, document_node: GraphNodeRecord) -> None:
        sections = self._section_nodes()
        self._section_relations(document_node, sections)
        tables = self._tables(document_node, sections)
        comments = self._comments(document_node, sections)
        self._chunk_nodes(document_node, sections, tables, comments)

    def _section_nodes(
        self,
    ) -> dict[tuple[str, ...], GraphNodeRecord]:
        evidence: defaultdict[tuple[str, ...], list[ChunkRecord]] = defaultdict(list)
        logical_ids: dict[tuple[str, ...], str] = {}
        for chunk in self._chunks:
            path = chunk.hierarchy.section_path
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                evidence[prefix].append(chunk)
                logical_ids.setdefault(
                    prefix,
                    self._section_logical_id(
                        chunk,
                        path=prefix,
                        terminal=depth == len(path),
                    ),
                )
        nodes = {
            path: self._state.structure_node(
                GraphEntityType.SECTION,
                logical_id,
                title=path[-1],
                section_path=path,
            )
            for path, logical_id in logical_ids.items()
        }
        return nodes

    def _section_relations(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
    ) -> None:
        for path, section in sections.items():
            if len(path) == 1:
                relation_type = RelationType.CONTAINS
                source, target = document_node, section
            else:
                relation_type = RelationType.PARENT_OF
                source, target = sections[path[:-1]], section
            self._state.relation(
                GraphRelationSpec(
                    relation_type=relation_type,
                    source=source,
                    target=target,
                    source_explicit=False,
                )
            )

    def _tables(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
    ) -> dict[str, GraphNodeRecord]:
        table_chunks: defaultdict[str, list[ChunkRecord]] = defaultdict(list)
        for chunk in self._chunks:
            if chunk.chunk_kind == ChunkKind.TABLE:
                if chunk.table_locator is None:
                    raise HarborInvariantError("chunk.table_locator must not be None here")
                table_chunks[chunk.table_locator.table_id].append(chunk)
        nodes: dict[str, GraphNodeRecord] = {}
        for table_id, records in sorted(table_chunks.items()):
            table = self._state.structure_node(
                GraphEntityType.TABLE,
                table_id,
                title=(
                    self._metadata_text(records[0], "table_caption")
                    or self._table_title(records[0])
                ),
                section_path=records[0].hierarchy.section_path,
            )
            nodes[table_id] = table
            source = sections.get(records[0].hierarchy.section_path, document_node)
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.CONTAINS,
                    source=source,
                    target=table,
                    source_explicit=False,
                )
            )
        return nodes

    def _comments(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
    ) -> dict[str, GraphNodeRecord]:
        comments = tuple(chunk for chunk in self._chunks if chunk.chunk_kind == ChunkKind.COMMENT)
        nodes: dict[str, GraphNodeRecord] = {}
        comment_ids: dict[str, str] = {}
        for chunk in comments:
            comment_id = self._comment_id(chunk)
            comment_ids[str(chunk.chunk_id)] = comment_id
            comment = self._state.structure_node(
                GraphEntityType.COMMENT,
                comment_id,
                title=f"Comment {comment_id}",
                section_path=chunk.hierarchy.section_path,
            )
            nodes[comment_id] = comment
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.CONTAINS,
                    source=document_node,
                    target=comment,
                    source_explicit=False,
                )
            )
            section = sections.get(chunk.hierarchy.section_path)
            if section is not None:
                self._state.relation(
                    GraphRelationSpec(
                        relation_type=RelationType.LINKS_TO,
                        source=comment,
                        target=section,
                        source_explicit=True,
                    )
                )
        for chunk in comments:
            parent_id = self._metadata_text(chunk, "parent_comment_id")
            comment_id = comment_ids[str(chunk.chunk_id)]
            if parent_id is None or parent_id not in nodes:
                continue
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.REPLY_TO,
                    source=nodes[comment_id],
                    target=nodes[parent_id],
                    source_explicit=True,
                )
            )
        return {
            chunk_id: nodes[comment_id]
            for chunk_id, comment_id in comment_ids.items()
        }

    def _chunk_nodes(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
        tables: dict[str, GraphNodeRecord],
        comments: dict[str, GraphNodeRecord],
    ) -> None:
        """Link vector evidence IDs to their nearest graph structure."""

        for chunk in self._chunks:
            if chunk.record_kind != RecordKind.EVIDENCE:
                continue
            chunk_id = str(chunk.chunk_id)
            target = comments.get(chunk_id)
            if target is None and chunk.table_locator is not None:
                target = tables.get(chunk.table_locator.table_id)
            if target is None:
                target = sections.get(chunk.hierarchy.section_path, document_node)
            chunk_node = self._state.node(
                GraphNodeSpec(
                    kind=KnowledgeNodeKind.CHUNK,
                    entity_type=GraphEntityType.CHUNK,
                    logical_id=chunk_id,
                    node_key=chunk_id,
                    ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
                    source_scope_id=chunk.source_scope_id,
                    document_id=chunk.document_id,
                    document_version_id=chunk.document_version_id,
                    section_path=chunk.hierarchy.section_path,
                    attributes={"ordinal": chunk.ordinal},
                )
            )
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.SUPPORTS,
                    source=chunk_node,
                    target=target,
                    source_explicit=False,
                )
            )

    @staticmethod
    def _section_logical_id(
        chunk: ChunkRecord,
        *,
        path: tuple[str, ...],
        terminal: bool,
    ) -> str:
        if terminal and chunk.hierarchy.section_id is not None:
            return chunk.hierarchy.section_id
        if (
            len(path) + 1 == len(chunk.hierarchy.section_path)
            and chunk.hierarchy.parent_section_id is not None
        ):
            return chunk.hierarchy.parent_section_id
        return encoded_identifier(
            "section",
            {"document_id": str(chunk.document_id), "section_path": path},
        )

    @classmethod
    def _comment_id(cls, chunk: ChunkRecord) -> str:
        return (
            cls._metadata_text(chunk, "comment_id")
            or next(iter(chunk.citation_locator.source_element_ids), None)
            or str(chunk.logical_chunk_id)
        )

    @staticmethod
    def _table_title(chunk: ChunkRecord) -> str:
        section = chunk.hierarchy.section_path[-1] if chunk.hierarchy.section_path else None
        return f"Table — {section}" if section else "Table"

    @staticmethod
    def _metadata_text(chunk: ChunkRecord, key: str) -> str | None:
        value = chunk.metadata.get(key)
        return str(value).strip() if value is not None and str(value).strip() else None
