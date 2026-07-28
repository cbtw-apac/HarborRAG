from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.schemas.documents import ChunkRecord

from ..schemas import IndexingRequest
from .capsule import ContextCapsuleBuilder
from .projectionstate import GraphProjectionState
from .sources import SourceGraphProjector


class UniversalGraphProjector:
    """Project canonical chunks into deterministic universal and explicit source graphs."""

    def __init__(
        self,
        capsule_builder: ContextCapsuleBuilder | None = None,
        source_projector: SourceGraphProjector | None = None,
    ) -> None:
        """Initialize capsule and source-specific graph projectors."""

        self._capsules = capsule_builder or ContextCapsuleBuilder()
        self._sources = source_projector or SourceGraphProjector()

    def project(
        self,
        request: IndexingRequest,
        *,
        vector_point_ids: Mapping[str, str],
    ) -> tuple[GraphProjectionState, tuple[str, ...]]:
        """Project canonical chunks into universal and source graph records."""

        chunking = request.chunking
        manifest = chunking.manifest
        records = tuple(sorted(chunking.chunks, key=lambda record: record.ordinal))
        self._validate_order(records)
        state = GraphProjectionState(
            namespace=request.config.graph_namespace,
            tenant_id=manifest.tenant_id,
            generation_id=request.generation_id,
            artifact_id=manifest.artifact_id,
            artifact_revision_id=manifest.artifact_revision_id,
        )
        source_kind = self._source_kind(records)
        artifact_labels = {"Artifact"}
        if source_kind == "jira":
            artifact_labels.add("JiraIssue")
        elif source_kind == "confluence":
            artifact_labels.add("ConfluencePage")
        else:
            artifact_labels.add("Document")
        artifact = state.node(
            kind="artifact",
            key=manifest.artifact_id,
            labels=artifact_labels,
            properties={"source_kind": source_kind},
        )
        revision = state.node(
            kind="revision",
            key=manifest.artifact_revision_id,
            labels={"Revision"},
            properties={"chunk_manifest_fingerprint": manifest.fingerprint},
        )
        state.edge("HAS_REVISION", artifact, revision)

        sections: dict[tuple[str, ...], str] = {}
        chunk_nodes: dict[str, str] = {}
        parent_nodes: dict[str, str] = {}
        references = {item.chunk_revision_id: item for item in manifest.chunks}
        for record in records:
            parent = self._sections(
                state,
                revision,
                record.context.structural_path,
                sections,
            )
            revision_id = str(record.chunk_revision_id)
            parent_nodes[revision_id] = parent
            reference = references[revision_id]
            chunk = state.node(
                kind="chunk",
                key=revision_id,
                labels=self._chunk_labels(record),
                properties=self._capsules.build(
                    record,
                    generation_id=request.generation_id,
                    vector_point_id=vector_point_ids.get(revision_id),
                    content_reference=(
                        reference.body_uri or f"harborrag:chunk:{record.chunk_revision_id!s}"
                    ),
                    config=request.config,
                ),
            )
            chunk_nodes[revision_id] = chunk
            state.edge("HAS_CHUNK", parent, chunk, qualifier=revision_id)

        self._chunk_order(state, records, chunk_nodes)
        self._sources.project(
            state,
            records,
            artifact_node_id=artifact,
            chunk_node_ids=chunk_nodes,
            parent_node_ids=parent_nodes,
        )
        return state, tuple(chunk_nodes[str(record.chunk_revision_id)] for record in records)

    @staticmethod
    def _sections(
        state: GraphProjectionState,
        revision_id: str,
        path: tuple[str, ...],
        sections: dict[tuple[str, ...], str],
    ) -> str:
        parent = revision_id
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            section = sections.get(prefix)
            if section is None:
                section = state.node(
                    kind="section",
                    key="\x1f".join(prefix),
                    labels={"Section"},
                    properties={
                        "title": prefix[-1],
                        "structural_path": list(prefix),
                        "depth": depth,
                    },
                )
                sections[prefix] = section
                relationship = "HAS_SECTION" if depth == 1 else "HAS_SUBSECTION"
                state.edge(relationship, parent, section)
            parent = section
        return parent

    @staticmethod
    def _chunk_order(
        state: GraphProjectionState,
        records: tuple[ChunkRecord, ...],
        chunk_nodes: Mapping[str, str],
    ) -> None:
        for previous, current in zip(records, records[1:], strict=False):
            previous_id = chunk_nodes[str(previous.chunk_revision_id)]
            current_id = chunk_nodes[str(current.chunk_revision_id)]
            state.edge("NEXT_CHUNK", previous_id, current_id)
            state.edge("PREVIOUS_CHUNK", current_id, previous_id)

    @staticmethod
    def _validate_order(records: tuple[ChunkRecord, ...]) -> None:
        ordinals = [record.ordinal for record in records]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("graph projection requires unique chunk ordinals")

    @staticmethod
    def _source_kind(records: tuple[ChunkRecord, ...]) -> str:
        if not records:
            return "unknown"
        value = records[0].metadata.get("source_kind", "unknown")
        return value.strip() if isinstance(value, str) and value.strip() else "unknown"

    @staticmethod
    def _chunk_labels(record: ChunkRecord) -> set[str]:
        labels = {"Chunk"}
        if record.role == "table":
            labels.add("Table")
        elif record.role in {"figure", "caption"}:
            labels.add("Figure")
        elif record.role == "jira.comment":
            labels.add("JiraComment")
        return labels
