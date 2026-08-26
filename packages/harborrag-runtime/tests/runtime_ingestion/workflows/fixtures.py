"""Workflow contracts shared by ingestion workflow tests."""

from __future__ import annotations

from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    WorkflowArtifactReference,
)


def source_input() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="chunks-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
        batch_size=2,
    )


def plan_reference() -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        bucket="harborrag-artifacts",
        key="source-plans/task-1/scan-1.json",
        sha256="a" * 64,
        byte_size=128,
        media_type="application/json",
    )
