"""Policy for selecting stale reindex lanes."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ingestion import ProcessingProfile


@dataclass(frozen=True, slots=True)
class ReindexPlan:
    """Minimal connector-free rebuild selected from profile drift.

    Args:
        rebuild_chunks: Chunk again from the canonical document.
        regenerate_dense: Embed the chunks again.
        regenerate_sparse: Compute the sparse representation again.
        rebuild_graph: Rebuild the graph projection.
        rebuild_vector_projection: Rebuild the vector projection.
    """

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
        """Select the lanes whose profile field differs between two profiles.

        Args:
            current: Profile the stored version was built with.
            target: Profile the reindex moves to.

        Returns:
            The lanes to rebuild.

        Raises:
            ValueError: The parser profile or normalizer version differs, which
                needs a raw-artifact replay instead of a connector-free rebuild.
        """
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
    """Validate the durable profile persisted beside canonical content.

    Args:
        value: Profile payload read from the canonical artifact.

    Returns:
        The validated processing profile.

    Raises:
        ValueError: The payload is not a mapping, so there is no reusable profile.
    """
    if not isinstance(value, dict):
        raise ValueError("canonical artifact has no reusable processing profile")
    return ProcessingProfile.model_validate(value)
