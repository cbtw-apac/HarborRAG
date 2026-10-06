from __future__ import annotations

import pytest

from harborrag_core.ingestion import (
    DocumentVersionState,
    FailureCategory,
    ProjectionWriteError,
    PublicationConflictError,
    SourceAdmissionDecision,
    SourceItemNotFoundError,
    SourceUnavailableError,
    is_explicitly_retryable_failure_code,
    is_retryable_failure_code,
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
    assert policy.already_reached(
        DocumentVersionState.ACTIVE,
        DocumentVersionState.CANONICAL_READY,
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
    policy.require_publishable(
        decision=SourceAdmissionDecision.FORCE_REPROCESS,
        state=DocumentVersionState.ACTIVE,
        requires_processing=True,
    )
    with pytest.raises(PublicationConflictError):
        policy.require_publishable(
            decision=SourceAdmissionDecision.NEW,
            state=DocumentVersionState.ACTIVE,
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


def test_publication_policy_treats_a_retried_publish_of_the_active_version_as_replay() -> None:
    policy = PublicationPolicy()

    policy.require_publishable(
        decision=SourceAdmissionDecision.NEW,
        state=DocumentVersionState.ACTIVE,
        requires_processing=True,
        is_current_active=True,
    )
    with pytest.raises(PublicationConflictError):
        policy.require_publishable(
            decision=SourceAdmissionDecision.NEW,
            state=DocumentVersionState.ACTIVE,
            requires_processing=True,
        )


class _ObjectStoreUnavailable(Exception):
    """Stands in for an adapter error the engine cannot import (e.g. botocore)."""


def test_failure_classifier_retries_infrastructure_faults_in_every_stage() -> None:
    classifier = IngestionFailureClassifier(transient_errors=(_ObjectStoreUnavailable,))

    for stage in ("ParseAndNormalize", "PersistCanonical", "ChunkAndValidate"):
        failure = classifier.classify(stage, _ObjectStoreUnavailable("storage full"))
        assert failure.retryable is True
        assert failure.category == FailureCategory.TRANSIENT
        try:
            raise RuntimeError("parse failed") from TimeoutError("ocr timed out")
        except RuntimeError as wrapped:
            assert classifier.classify(stage, wrapped).retryable is True

    # A domain verdict still wins, and an error merely raised while handling a
    # timeout is not treated as the timeout.
    assert classifier.classify("ChunkAndValidate", ChunkValidationError("bad")).retryable is False
    try:
        try:
            raise TimeoutError("ocr timed out")
        except TimeoutError:
            raise ValueError("invalid document") from None
    except ValueError as handled:
        assert classifier.classify("ParseAndNormalize", handled).retryable is False


def test_retry_selection_codes_separate_blanket_and_explicit_retries() -> None:
    # Outages and replay conflicts qualify for a blanket retry in any stage.
    assert is_retryable_failure_code("document_release_fetchandcaptureraw_clienterror")
    assert is_retryable_failure_code("document_release_persistcanonical_operationalerror")
    assert is_retryable_failure_code("document_release_immutable_artifact_conflict")
    assert is_retryable_failure_code("document_release_publication_conflict")
    # Deterministic failures only when an operator names the document.
    assert not is_retryable_failure_code("document_release_chunk_invalid")
    assert is_explicitly_retryable_failure_code("document_release_chunk_invalid")
    # Failures inherent to the source are never offered.
    for code in ("document_release_document_unsupported", "parser_rejected_document"):
        assert not is_retryable_failure_code(code)
        assert not is_explicitly_retryable_failure_code(code)
    assert not is_retryable_failure_code("  ")


def test_a_deleted_source_item_is_not_retried_as_an_outage() -> None:
    # Jira answers 404 for an issue deleted after discovery listed it; that used to
    # be "source_unavailable", retried five times, then counted as failed.
    class _GoneIssue(SourceItemNotFoundError, SourceUnavailableError):
        """Like the connector's ItemNotFoundError: a fetch error that is a 404."""

    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", _GoneIssue("404"))

    assert failure.code == "source_item_not_found"
    assert failure.retryable is False
    assert not is_retryable_failure_code("document_release_source_item_not_found")
    assert is_explicitly_retryable_failure_code("document_release_source_item_not_found")
