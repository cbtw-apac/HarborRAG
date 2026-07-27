"""Normalize child-partition failures into deterministic result values."""

from __future__ import annotations

from typing import Any, cast

from temporalio.exceptions import ChildWorkflowError
from temporalio.workflow import ChildWorkflowHandle

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
    ArtifactResult,
    ArtifactStatus,
    PartitionResult,
    RunProgress,
)


async def partition_result(
    handle: ChildWorkflowHandle[PartitionResult, Any],
    number: int,
    artifacts: tuple[ArtifactReference, ...],
) -> PartitionResult:
    try:
        return cast("PartitionResult", await handle)
    except ChildWorkflowError:
        return PartitionResult(
            partition_number=number,
            progress=RunProgress(
                processed=len(artifacts),
                failed=len(artifacts),
            ),
            failed_artifacts=tuple(item.artifact_id for item in artifacts),
            artifact_results=tuple(
                ArtifactResult(
                    artifact_id=item.artifact_id,
                    artifact_revision_id=item.artifact_revision_id,
                    status=ArtifactStatus.FAILED,
                    error_type="ChildWorkflowError",
                    error_message=(
                        "operation failed; inspect restricted worker logs "
                        "using workflow identifiers"
                    ),
                )
                for item in artifacts
            ),
        )
