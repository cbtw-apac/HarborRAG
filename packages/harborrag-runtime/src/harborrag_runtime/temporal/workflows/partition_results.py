"""Normalize child-partition failures into deterministic result values."""

from __future__ import annotations

from typing import Any, cast

from temporalio.exceptions import ChildWorkflowError
from temporalio.workflow import ChildWorkflowHandle

from harborrag_runtime.temporal.schemas import (
    ArtifactReference,
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
        )
