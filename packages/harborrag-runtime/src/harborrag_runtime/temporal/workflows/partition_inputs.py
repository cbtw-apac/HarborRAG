"""Construct child-partition workflow input from a run request."""

from __future__ import annotations

from dataclasses import replace

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    IngestionRunInput,
    PartitionInput,
)


def build_partition_input(
    request: IngestionRunInput,
    number: int,
    artifacts: tuple[ArtifactReference, ...],
    artifact_concurrency: int,
    *,
    attempt: int = 0,
) -> PartitionInput:
    options = replace(
        request.options,
        artifact_concurrency=artifact_concurrency,
    )
    return PartitionInput(
        run_id=request.run_id,
        tenant_id=request.tenant_id,
        manifest_id=request.manifest_id,
        partition_number=number,
        artifacts=artifacts,
        generation_id=request.generation_id,
        options=options,
        attempt=attempt,
    )
