from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion import (
    ChunkValidationError,
    DocumentVersionState,
    FailureCategory,
    ParserRejectedDocumentError,
    ProjectionVerificationError,
    ProjectionWriteError,
    PublicationConflictError,
    RepresentationProviderError,
    SourceAdmissionDecision,
    SourceForbiddenError,
    SourceUnavailableError,
    UnsupportedDocumentError,
)

_DURABLE_STAGES = (
    DocumentVersionState.PENDING,
    DocumentVersionState.RAW_CAPTURED,
    DocumentVersionState.CANONICAL_READY,
    DocumentVersionState.CHUNKS_READY,
    DocumentVersionState.REPRESENTATIONS_READY,
    DocumentVersionState.PROJECTIONS_STAGED,
    DocumentVersionState.VERIFIED,
)
_STAGE_RANK = {state: rank for rank, state in enumerate(_DURABLE_STAGES)}
_STAGE_CATEGORIES = {
    "ParseAndNormalize": FailureCategory.CANONICAL_VALIDATION,
    "PersistCanonical": FailureCategory.CANONICAL_VALIDATION,
    "ChunkAndValidate": FailureCategory.CHUNK_VALIDATION,
    "EncodeChunks": FailureCategory.ENCODER_FAILURE,
    "WriteVectorProjection": FailureCategory.VECTOR_WRITE_FAILURE,
    "WriteGraphProjection": FailureCategory.GRAPH_WRITE_FAILURE,
    "VerifyProjections": FailureCategory.VERIFICATION_FAILURE,
    "PublishVersion": FailureCategory.PUBLICATION_FAILURE,
}
_SIMPLE_FAILURES: tuple[
    tuple[type[Exception], FailureCategory, bool, str],
    ...,
] = (
    (SourceForbiddenError, FailureCategory.SOURCE_FORBIDDEN, False, "source_forbidden"),
    (SourceUnavailableError, FailureCategory.TRANSIENT, True, "source_unavailable"),
    (UnsupportedDocumentError, FailureCategory.UNSUPPORTED, False, "document_unsupported"),
    (
        ParserRejectedDocumentError,
        FailureCategory.CANONICAL_VALIDATION,
        False,
        "parser_rejected_document",
    ),
    (ChunkValidationError, FailureCategory.CHUNK_VALIDATION, False, "chunk_invalid"),
    (
        RepresentationProviderError,
        FailureCategory.ENCODER_FAILURE,
        True,
        "representation_failed",
    ),
    (
        ProjectionVerificationError,
        FailureCategory.VERIFICATION_FAILURE,
        False,
        "projection_verification_failed",
    ),
)


class DocumentVersionTransitionPolicy:
    """Validate provider-independent state transitions and replay boundaries."""

    def allows(self, current: DocumentVersionState, target: DocumentVersionState) -> bool:
        if current == target:
            return True
        if target == DocumentVersionState.FAILED:
            return current not in {
                DocumentVersionState.ACTIVE,
                DocumentVersionState.RETIRED,
                DocumentVersionState.FAILED,
            }
        if current == DocumentVersionState.VERIFIED and target == DocumentVersionState.ACTIVE:
            return True
        if current == DocumentVersionState.ACTIVE and target == DocumentVersionState.RETIRED:
            return True
        if (
            current == DocumentVersionState.PENDING
            and target == DocumentVersionState.CANONICAL_READY
        ):
            # Connector-free reindex starts from an already authoritative
            # immutable canonical artifact, so there is no raw-capture stage.
            return True
        current_rank = _STAGE_RANK.get(current)
        target_rank = _STAGE_RANK.get(target)
        return current_rank is not None and target_rank == current_rank + 1

    def already_reached(
        self,
        current: DocumentVersionState,
        target: DocumentVersionState,
    ) -> bool:
        """Return whether an idempotent replay has passed the requested stage."""

        current_rank = _STAGE_RANK.get(current)
        target_rank = _STAGE_RANK.get(target)
        return current_rank is not None and target_rank is not None and current_rank > target_rank

    def require(self, current: DocumentVersionState, target: DocumentVersionState) -> None:
        if not self.allows(current, target):
            raise PublicationConflictError(
                f"invalid document-version transition: {current.value} -> {target.value}"
            )


class PublicationPolicy:
    """Decide whether a candidate may enter the atomic publication transaction."""

    def requires_publication(self, decision: SourceAdmissionDecision) -> bool:
        return decision not in {
            SourceAdmissionDecision.UNCHANGED,
            SourceAdmissionDecision.UNSUPPORTED,
            SourceAdmissionDecision.SECURITY_REJECTED,
            SourceAdmissionDecision.REMOVED_CANDIDATE,
        }

    def require_publishable(
        self,
        *,
        decision: SourceAdmissionDecision,
        state: DocumentVersionState,
        requires_processing: bool,
    ) -> None:
        if not self.requires_publication(decision):
            raise PublicationConflictError(
                f"admission decision {decision.value} cannot publish a document version"
            )
        required = (
            {DocumentVersionState.VERIFIED}
            if requires_processing
            else {DocumentVersionState.PENDING, DocumentVersionState.VERIFIED}
        )
        if state not in required:
            expected = ", ".join(sorted(item.value for item in required))
            raise PublicationConflictError(
                f"document version in {state.value} cannot publish; expected {expected}"
            )


class CleanupPolicy:
    """Prevent cleanup from deleting the active authoritative projection."""

    def may_delete(self, *, document_version_id: str, active_version_id: str | None) -> bool:
        return active_version_id is None or active_version_id != document_version_id


@dataclass(frozen=True, slots=True)
class SafeFailure:
    category: FailureCategory
    retryable: bool
    code: str


class IngestionFailureClassifier:
    """Map normalized domain errors to safe durable failure information."""

    def classify(self, stage: str, error: Exception) -> SafeFailure:
        for error_type, category, retryable, code in _SIMPLE_FAILURES:
            if isinstance(error, error_type):
                return SafeFailure(category, retryable, code)
        if isinstance(error, ProjectionWriteError):
            return SafeFailure(
                _STAGE_CATEGORIES.get(stage, FailureCategory.TRANSIENT),
                True,
                "projection_write_failed",
            )
        if isinstance(error, PublicationConflictError):
            return SafeFailure(
                FailureCategory.PUBLICATION_FAILURE,
                False,
                "publication_conflict",
            )
        if isinstance(error, HarborConflictError):
            return SafeFailure(
                _STAGE_CATEGORIES.get(stage, FailureCategory.CANONICAL_VALIDATION),
                False,
                "immutable_artifact_conflict",
            )
        category = _STAGE_CATEGORIES.get(stage, FailureCategory.TRANSIENT)
        retryable = not isinstance(error, (KeyError, TypeError, ValueError)) and category not in {
            FailureCategory.CANONICAL_VALIDATION,
            FailureCategory.CHUNK_VALIDATION,
            FailureCategory.UNSUPPORTED,
        }
        return SafeFailure(category, retryable, f"{stage.lower()}_{type(error).__name__.lower()}")
