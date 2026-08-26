from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    DocumentVersionState,
    FailureCategory,
    ProjectionWriteError,
    PublicationConflictError,
    SourceAdmissionDecision,
)
from harborrag_engine.ingestion import (
    CleanupPolicy,
    DocumentVersionTransitionPolicy,
    IngestionFailureClassifier,
    PublicationPolicy,
)
from harborrag_engine.ingestion.chunking import ChunkValidationError


def test_document_version_policy_enforces_order_and_replay() -> None:
    policy = DocumentVersionTransitionPolicy()

    assert policy.allows(
        DocumentVersionState.CANONICAL_READY,
        DocumentVersionState.CHUNKS_READY,
    )
    assert policy.already_reached(
        DocumentVersionState.VERIFIED,
        DocumentVersionState.CHUNKS_READY,
    )
    with pytest.raises(PublicationConflictError):
        policy.require(
            DocumentVersionState.CANONICAL_READY,
            DocumentVersionState.ACTIVE,
        )


def test_publication_policy_requires_verified_mandatory_projections() -> None:
    policy = PublicationPolicy()

    policy.require_publishable(
        decision=SourceAdmissionDecision.NEW,
        state=DocumentVersionState.VERIFIED,
        requires_processing=True,
    )
    with pytest.raises(PublicationConflictError):
        policy.require_publishable(
            decision=SourceAdmissionDecision.NEW,
            state=DocumentVersionState.PROJECTIONS_STAGED,
            requires_processing=True,
        )


def test_cleanup_and_failure_policies_are_provider_independent() -> None:
    assert CleanupPolicy().may_delete(
        document_version_id="old",
        active_version_id="active",
    )
    assert not CleanupPolicy().may_delete(
        document_version_id="active",
        active_version_id="active",
    )

    failure = IngestionFailureClassifier().classify(
        "WriteVectorProjection",
        ProjectionWriteError("provider details"),
    )
    assert failure.retryable is True
    assert failure.code == "projection_write_failed"

    invalid_chunk = IngestionFailureClassifier().classify(
        "ChunkAndValidate",
        ChunkValidationError("invalid manifest"),
    )
    assert invalid_chunk.category == FailureCategory.CHUNK_VALIDATION
    assert invalid_chunk.retryable is False
    assert invalid_chunk.code == "chunk_invalid"
