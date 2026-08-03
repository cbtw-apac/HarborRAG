from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from harborrag_core.chunking import ChunkKind, ChunkRecord, RelationType
from harborrag_core.chunking.identity import encoded_identifier
from harborrag_core.ingestion import GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.invariants import HarborInvariantError

from .graph_state import GraphProjectionState, GraphRelationSpec

_NODE_PREVIEW_CHARACTERS = 640


def reviewable_preview(chunks: Sequence[ChunkRecord]) -> str | None:
    """Build bounded, de-duplicated node content for graph inspection."""

    parts = tuple(dict.fromkeys(chunk.content.strip() for chunk in chunks if chunk.content.strip()))
    if not parts:
        return None
    content = "\n\n".join(parts)
    return content[:_NODE_PREVIEW_CHARACTERS]


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
        sections, evidence = self._section_nodes()
        self._section_relations(document_node, sections, evidence)
        self._tables(document_node, sections)
        self._comments(document_node, sections)

    def _section_nodes(
        self,
    ) -> tuple[
        dict[tuple[str, ...], GraphNodeRecord],
        dict[tuple[str, ...], tuple[str, ...]],
    ]:
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
            path: self._state.current_node(
                KnowledgeNodeKind.SECTION,
                logical_id,
                title=path[-1],
                content_preview=reviewable_preview(evidence[path]),
                section_path=path,
            )
            for path, logical_id in logical_ids.items()
        }
        return (
            nodes,
            {
                path: tuple(dict.fromkeys(str(chunk.chunk_id) for chunk in chunks))
                for path, chunks in evidence.items()
            },
        )

    def _section_relations(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
        evidence: dict[tuple[str, ...], tuple[str, ...]],
    ) -> None:
        for path, section in sections.items():
            if len(path) == 1:
                relation_type = RelationType.HAS_SECTION
                source, target = document_node, section
            else:
                relation_type = RelationType.CHILD_OF
                source, target = section, sections[path[:-1]]
            self._state.relation(
                GraphRelationSpec(
                    relation_type=relation_type,
                    source=source,
                    target=target,
                    source_explicit=False,
                    evidence_chunk_ids=evidence[path],
                )
            )

    def _tables(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
    ) -> None:
        table_chunks: defaultdict[str, list[ChunkRecord]] = defaultdict(list)
        for chunk in self._chunks:
            if chunk.chunk_kind == ChunkKind.TABLE:
                if chunk.table_locator is None:
                    raise HarborInvariantError("chunk.table_locator must not be None here")
                table_chunks[chunk.table_locator.table_id].append(chunk)
        for table_id, records in sorted(table_chunks.items()):
            table = self._state.current_node(
                KnowledgeNodeKind.TABLE,
                table_id,
                title=(
                    self._metadata_text(records[0], "table_caption")
                    or self._table_title(records[0])
                ),
                content_preview=reviewable_preview(records),
                section_path=records[0].hierarchy.section_path,
            )
            source = sections.get(records[0].hierarchy.section_path, document_node)
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.HAS_TABLE,
                    source=source,
                    target=table,
                    source_explicit=False,
                    evidence_chunk_ids=tuple(str(chunk.chunk_id) for chunk in records),
                )
            )

    def _comments(
        self,
        document_node: GraphNodeRecord,
        sections: dict[tuple[str, ...], GraphNodeRecord],
    ) -> None:
        comments = tuple(chunk for chunk in self._chunks if chunk.chunk_kind == ChunkKind.COMMENT)
        nodes: dict[str, GraphNodeRecord] = {}
        comment_ids: dict[str, str] = {}
        for chunk in comments:
            comment_id = self._comment_id(chunk)
            comment_ids[str(chunk.chunk_id)] = comment_id
            comment = self._state.current_node(
                KnowledgeNodeKind.COMMENT,
                comment_id,
                title=self._comment_title(chunk),
                content_preview=reviewable_preview((chunk,)),
                section_path=chunk.hierarchy.section_path,
            )
            nodes[comment_id] = comment
            evidence = (str(chunk.chunk_id),)
            self._state.relation(
                GraphRelationSpec(
                    relation_type=RelationType.HAS_COMMENT,
                    source=document_node,
                    target=comment,
                    source_explicit=False,
                    evidence_chunk_ids=evidence,
                )
            )
            section = sections.get(chunk.hierarchy.section_path)
            if section is not None:
                self._state.relation(
                    GraphRelationSpec(
                        relation_type=RelationType.COMMENT_ON,
                        source=comment,
                        target=section,
                        source_explicit=True,
                        evidence_chunk_ids=evidence,
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
                    evidence_chunk_ids=(str(chunk.chunk_id),),
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
    def _comment_title(chunk: ChunkRecord) -> str:
        first_line = next(
            (line.strip() for line in chunk.content.splitlines() if line.strip()),
            "Comment",
        )
        return first_line[:120]

    @staticmethod
    def _metadata_text(chunk: ChunkRecord, key: str) -> str | None:
        value = chunk.metadata.get(key)
        return str(value).strip() if value is not None and str(value).strip() else None
