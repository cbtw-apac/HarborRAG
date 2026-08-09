"""Policy for selecting stale reindex lanes."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ingestion import ProcessingProfile


@dataclass(frozen=True, slots=True)
class ReindexPlan:
    """Minimal connector-free rebuild selected from profile drift."""

    rebuild_chunks: bool
    regenerate_dense: bool
    regenerate_sparse: bool
    rebuild_graph: bool
    rebuild_vector_projection: bool

    @classmethod
    def between(
        cls,
        current: ProcessingProfile,
        target: ProcessingProfile,
    ) -> ReindexPlan:
        if current.parser_profile != target.parser_profile:
            raise ValueError("parser profile changes require a raw-artifact replay")
        if current.normalizer_version != target.normalizer_version:
            raise ValueError("normalizer changes require a raw-artifact replay")
        chunks = current.chunk_strategy != target.chunk_strategy
        dense = chunks or current.dense_encoder_profile != target.dense_encoder_profile
        sparse = chunks or current.sparse_encoder_profile != target.sparse_encoder_profile
        graph = chunks or current.graph_projection_version != target.graph_projection_version
        vectors = (
            dense or sparse or current.vector_projection_schema != target.vector_projection_schema
        )
        return cls(
            rebuild_chunks=chunks,
            regenerate_dense=dense,
            regenerate_sparse=sparse,
            rebuild_graph=graph,
            rebuild_vector_projection=vectors,
        )


def processing_profile_from_canonical(value: object) -> ProcessingProfile:
    """Validate the durable profile persisted beside canonical content."""

    if not isinstance(value, dict):
        raise ValueError("canonical artifact has no reusable processing profile")
    return ProcessingProfile.model_validate(value)
