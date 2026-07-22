"""Deterministic external workflow identities."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from harborrag_runtime.temporal.models import PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class RuntimeWorkflowRef:
    run_id: str
    workflow_id: str
    first_execution_run_id: str | None
    version: int = PAYLOAD_VERSION


def ingestion_workflow_id(run_id: str) -> str:
    return f"harborrag-ingestion-{_identifier(run_id)}"


def partition_workflow_id(run_id: str, partition_number: int, attempt: int) -> str:
    return f"harborrag-partition-{_identifier(run_id)}-{partition_number}-{attempt}"


def artifact_workflow_id(run_id: str, artifact_id: str, attempt: int) -> str:
    return f"harborrag-artifact-{_identifier(run_id)}-{_identifier(artifact_id)}-{attempt}"


def _identifier(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]
