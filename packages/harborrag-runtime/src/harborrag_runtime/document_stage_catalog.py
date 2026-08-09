"""Pure, sandbox-safe catalog of ordered document processing stages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentStageSpec:
    name: str
    activity: str
    task_queue: str


DOCUMENT_STAGE_CATALOG = (
    DocumentStageSpec("SyncContentUnits", "harborrag.sync_content_units", "harborrag-transform"),
    DocumentStageSpec("PersistCanonical", "harborrag.persist_canonical", "harborrag-io"),
    DocumentStageSpec("ChunkAndValidate", "harborrag.chunk_and_validate", "harborrag-transform"),
    DocumentStageSpec("EncodeChunks", "harborrag.encode_chunks", "harborrag-model"),
    DocumentStageSpec("BuildRelations", "harborrag.build_relations", "harborrag-transform"),
    DocumentStageSpec("BuildProjections", "harborrag.build_projections", "harborrag-transform"),
    DocumentStageSpec(
        "WriteVectorProjection", "harborrag.write_vector_projection", "harborrag-index"
    ),
    DocumentStageSpec(
        "WriteGraphProjection", "harborrag.write_graph_projection", "harborrag-index"
    ),
    DocumentStageSpec("VerifyProjections", "harborrag.verify_projections", "harborrag-index"),
)

__all__ = ["DOCUMENT_STAGE_CATALOG", "DocumentStageSpec"]
