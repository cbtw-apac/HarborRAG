"""Pure, sandbox-safe catalog of ordered document processing stages."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.temporal_models import TaskQueueRole


@dataclass(frozen=True, slots=True)
class DocumentStageSpec:
    name: str
    activity: str
    task_queue: str

    @property
    def method_name(self) -> str:
        """Return the matching ``IngestionActivities`` method name."""
        return self.activity.removeprefix("harborrag.")

    @property
    def task_queue_role(self) -> TaskQueueRole:
        """Return the logical lane represented by the stable default queue."""
        roles: dict[str, TaskQueueRole] = {
            "harborrag-transform": "transform",
            "harborrag-io": "io",
            "harborrag-model": "model",
            "harborrag-index": "index",
        }
        try:
            return roles[self.task_queue]
        except KeyError as exc:
            raise ValueError(f"unknown document-stage task queue: {self.task_queue}") from exc


DOCUMENT_STAGE_CATALOG = (
    DocumentStageSpec(
        "SyncContentUnits",
        "harborrag.sync_content_units",
        "harborrag-transform",
    ),
    DocumentStageSpec(
        "PersistCanonical",
        "harborrag.persist_canonical",
        "harborrag-io",
    ),
    DocumentStageSpec(
        "ChunkAndValidate",
        "harborrag.chunk_and_validate",
        "harborrag-transform",
    ),
    DocumentStageSpec(
        "EncodeChunks",
        "harborrag.encode_chunks",
        "harborrag-model",
    ),
    DocumentStageSpec(
        "BuildRelations",
        "harborrag.build_relations",
        "harborrag-transform",
    ),
    DocumentStageSpec(
        "BuildProjections",
        "harborrag.build_projections",
        "harborrag-transform",
    ),
    DocumentStageSpec(
        "WriteVectorProjection",
        "harborrag.write_vector_projection",
        "harborrag-index",
    ),
    DocumentStageSpec(
        "WriteGraphProjection",
        "harborrag.write_graph_projection",
        "harborrag-index",
    ),
    DocumentStageSpec(
        "VerifyProjections",
        "harborrag.verify_projections",
        "harborrag-index",
    ),
)

__all__ = ["DOCUMENT_STAGE_CATALOG", "DocumentStageSpec"]
