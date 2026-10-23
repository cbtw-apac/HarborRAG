from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborRateLimitError,
    HarborUnavailableError,
)
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
    SourceAuthenticationError,
    SourceAuthorizationError,
    SourceForbiddenError,
    SourceItemNotFoundError,
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
    # More specific than SourceForbiddenError below; order matters since the
    # first isinstance match wins and both are SourceForbiddenError subclasses.
    (
        SourceAuthenticationError,
        FailureCategory.SOURCE_FORBIDDEN,
        False,
        "authentication_failed",
    ),
    (
        SourceAuthorizationError,
        FailureCategory.SOURCE_FORBIDDEN,
        False,
        "authorization_failed",
    ),
    (SourceForbiddenError, FailureCategory.SOURCE_FORBIDDEN, False, "source_forbidden"),
    # Before SourceUnavailableError: a connector's not-found error is also a fetch
    # error, and a deleted item answers the same way however often it is asked.
    (
        SourceItemNotFoundError,
        FailureCategory.SOURCE_FORBIDDEN,
        False,
        "source_item_not_found",
    ),
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


_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborRateLimitError,
    HarborUnavailableError,
    ConnectionError,
    TimeoutError,
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
                DocumentVersionState.PURGING,
                DocumentVersionState.PURGED,
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

        if current == DocumentVersionState.ACTIVE and target in _STAGE_RANK:
            return True
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
        is_current_active: bool = False,
    ) -> None:
        if not self.requires_publication(decision):
            raise PublicationConflictError(
                f"admission decision {decision.value} cannot publish a document version"
            )
        if state == DocumentVersionState.ACTIVE and is_current_active:
            # A retried publish whose first attempt committed: the publisher
            # answers this as an idempotent replay, so it is not a conflict.
            return
        if requires_processing:
            required = {DocumentVersionState.VERIFIED}
            if decision == SourceAdmissionDecision.FORCE_REPROCESS:
                required.add(DocumentVersionState.ACTIVE)
        else:
            required = {DocumentVersionState.PENDING, DocumentVersionState.VERIFIED}
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
    """Map normalized domain errors to safe durable failure information.

    Infrastructure failures -- a dropped connection, a timeout, a full or
    unavailable object store -- are transient in every stage. Before, any
    unexpected exception in a parse, persist or chunk stage counted as a
    validation failure and was never retried, so a short MinIO or database
    outage failed documents permanently. Adapter-level types the engine cannot
    import (storage, S3, SQL drivers) are supplied by the runtime.
    """

    def __init__(self, transient_errors: tuple[type[BaseException], ...] = ()) -> None:
        self._transient_errors = (*_TRANSIENT_ERRORS, *transient_errors)

    def classify(self, stage: str, error: Exception) -> SafeFailure:
        for error_type, category, retryable, code in _SIMPLE_FAILURES:
            if isinstance(error, error_type):
                return SafeFailure(category, retryable, code)
        if self._is_transient(error):
            return SafeFailure(
                FailureCategory.TRANSIENT,
                True,
                f"{stage.lower()}_{type(error).__name__.lower()}",
            )
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

    def _is_transient(self, error: BaseException) -> bool:
        """Whether ``error`` or an error it was explicitly raised from is an infrastructure fault.

        Only ``raise ... from`` chains count: an error merely raised while handling
        a timeout (``__context__``) is that handler's own verdict, not the timeout.
        """

        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen and len(seen) < 16:
            if isinstance(current, self._transient_errors):
                return True
            seen.add(id(current))
            current = current.__cause__
        return False
