from __future__ import annotations

import pytest
from harborrag_runtime.config.temporal import (
    TemporalRuntimeConfig,
    TemporalTLSConfig,
)
from harborrag_runtime.errors import RuntimeConfigurationError
from harborrag_runtime.temporal.models import ArtifactReference, WorkflowOptions
from harborrag_runtime.temporal.task_queues import ActivityClass, TaskQueueConfig


def test_task_queues_are_central_and_unique() -> None:
    queues = TaskQueueConfig()

    assert queues.for_activity(ActivityClass.DISCOVERY) == "harborrag-runtime-discovery"
    assert queues.for_activity(ActivityClass.INDEXING) == "harborrag-runtime-vector-index"
    assert len(set(queues.as_mapping().values())) == 8


def test_runtime_defaults_match_local_temporal_namespace() -> None:
    config = TemporalRuntimeConfig()

    assert config.connection.namespace == "harborrag"
    assert config.artifact_concurrency == 16
    assert config.workflow_options().artifact_concurrency == 16


def test_retry_policies_differ_by_activity_class() -> None:
    retries = TemporalRuntimeConfig().retries

    assert retries.parser.maximum_attempts < retries.indexing.maximum_attempts
    assert retries.parser.start_to_close_seconds < retries.ocr.start_to_close_seconds
    assert "AuthenticationError" in retries.connector.non_retryable_error_types
    assert "ParseError" in retries.parser.non_retryable_error_types


def test_workflow_options_validate_bounded_concurrency() -> None:
    with pytest.raises(ValueError, match="positive"):
        WorkflowOptions(artifact_concurrency=0)


def test_tls_rejects_partial_client_credentials() -> None:
    with pytest.raises(RuntimeConfigurationError, match="together"):
        TemporalTLSConfig(enabled=True, client_cert=b"cert")


def test_artifact_reference_is_compact_and_versioned() -> None:
    reference = ArtifactReference(
        artifact_id="document-1",
        source_ref="object://sources/1",
        source_kind="confluence",
        connector_name="docs",
    )

    assert reference.version == 1
    assert not hasattr(reference, "content")
    assert not hasattr(reference, "embedding")
