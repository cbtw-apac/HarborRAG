"""Vector-store report for the chunking smoke check.

The report contains one thing: the vector points this revision would write. Each
point carries the identity the collection would key on, the payload
`VectorPayloadBuilder` produces, and the embedding input whose vector would be
stored against it. Parser, routing, and chunking diagnostics are the check's own
progress output and go to stderr, not into this document.

Vector values are not present: computing them requires a real embedding
provider, which this check deliberately never calls.
"""

from __future__ import annotations

from typing import Any

from chunking_stage import GENERATION_ID, ChunkingStage, StageOutcome

from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.vector import VectorPoint
from harborrag_engine.ingestion.indexing import PreparedEmbeddingInput
from harborrag_engine.ingestion.indexing.vector.schemas import (
    VectorMutation,
    VectorMutationAction,
)


def vector_policy(stage: ChunkingStage) -> dict[str, Any]:
    """Describe the collection and embedding identity every point is keyed by."""

    config = stage.indexing_config
    return {
        "collection": config.vector_collection,
        "generation_id": GENERATION_ID,
        "distance": config.vector_distance.value,
        "dimension": config.embedding_dimensions,
        "metadata_indexes": list(config.vector_metadata_indexes),
        "payload_includes_content": config.include_chunk_content_in_vector_payload,
        "vectors_computed": False,
        "embedding": {
            "model": config.embedding_model,
            "identity_source": stage.embedding_identity_source,
            "configuration_fingerprint": config.embedding_configuration_fingerprint,
            "context_maximum_characters": config.embedding_context_maximum_characters,
            "text_rendering_version": config.embedding_text_rendering_version,
            "maximum_batch_tokens": config.maximum_embedding_batch_tokens,
        },
    }


def _embedding_input(
    prepared: PreparedEmbeddingInput,
    *,
    include_content: bool,
) -> dict[str, Any]:
    """Describe the text whose vector would be stored against this point.

    `EmbeddingInputPreparer` renders its own bounded context header and prepends
    it to the chunk content; `ChunkRecord.embedding_text` is not what the vector
    path sends. The header is always reported because it changes the vector; the
    full text is source content and stays behind `--include-content`.

    The header is the prepared text minus its content suffix, sliced by length
    rather than by `removesuffix` so an empty-content chunk cannot report its
    whole prepared text as the header.
    """

    content = prepared.record.content
    header = prepared.text[: len(prepared.text) - len(content)].rstrip() if content else ""
    entry: dict[str, Any] = {
        "context_header": header,
        "characters": len(prepared.text),
        "token_count": prepared.token_count,
    }
    if include_content:
        entry["text"] = prepared.text
    return entry


def _point_entry(
    mutation: VectorMutation,
    point: VectorPoint,
    prepared: PreparedEmbeddingInput,
    *,
    include_content: bool,
) -> dict[str, Any]:
    return {
        "action": mutation.action.value,
        "id": point.id,
        "tenant_id": str(point.tenant_id),
        "vector": None,
        "embedding_input": _embedding_input(prepared, include_content=include_content),
        "payload": point.payload,
    }


def document_report(
    outcome: StageOutcome,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """Render the vector points one record would write, and nothing else."""

    prepared_by_revision = {
        str(prepared.record.chunk_revision_id): prepared for prepared in outcome.prepared
    }
    points: list[dict[str, Any]] = []
    for mutation in outcome.plan.mutations:
        if mutation.action is not VectorMutationAction.UPSERT or mutation.point is None:
            continue
        revision = str(mutation.current_chunk_revision_id)
        prepared = prepared_by_revision.get(revision)
        if prepared is None:
            raise ValueError(f"planned point has no prepared embedding input: {revision}")
        points.append(
            _point_entry(mutation, mutation.point, prepared, include_content=include_content)
        )
    return {"record_id": outcome.record.id, "points": points}


def failure_report(record: SourceRecord, error: BaseException, detail: str) -> dict[str, Any]:
    """Name one record that produced no points, without leaking its content.

    `detail` is the bounded, redacted rendering the caller already printed; its
    leading type name is dropped because the type is reported separately.
    """

    error_type = type(error).__name__
    return {
        "record_id": record.id,
        "points": [],
        "error": {"type": error_type, "detail": detail.removeprefix(f"{error_type}: ")},
    }
