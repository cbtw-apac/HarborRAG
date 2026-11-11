"""Provider-independent ingestion rules: version transitions, publication gate and failure classes.

Pure rules over core states and errors; nothing here performs I/O or knows an adapter.
Adapter error types are supplied to the classifier by runtime.

Start from: DocumentVersionTransitionPolicy, PublicationPolicy, IngestionFailureClassifier.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborConnectionError,
    HarborDeadlineExceeded,
    HarborRateLimitError,
    HarborUnavailableError,
)
from harborrag_core.ingestion.errors import (
    ChunkValidationError,
    ParserRejectedDocumentError,
    ProjectionVerificationError,
    ProjectionWriteError,
    PublicationConflictError,
    RepresentationProviderError,
    SourceAuthenticationError,
    SourceAuthorizationError,
    SourceForbiddenError,
    SourceItemNotFoundError,
    SourceRetriesExhaustedError,
    SourceUnavailableError,
    UnsupportedDocumentError,
)
from harborrag_core.ingestion.states import (
    DocumentVersionState,
    FailureCategory,
    SourceAdmissionDecision,
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
type KnownFailure = tuple[type[Exception], FailureCategory, bool, str]
"""An error type with the category, retryable flag and safe code it classifies as.

The first isinstance match wins, so a subclass is listed before its parent:
``SourceAuthenticationError`` and ``SourceAuthorizationError`` before ``SourceForbiddenError``;
``SourceItemNotFoundError`` (a deleted item answers the same however often it is asked) and
``SourceRetriesExhaustedError`` (the connector spent its retry budget, so the run does not
retry it again) before ``SourceUnavailableError``.
"""

_SIMPLE_FAILURES: tuple[KnownFailure, ...] = (
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
    (
        SourceItemNotFoundError,
        FailureCategory.SOURCE_FORBIDDEN,
        False,
        "source_item_not_found",
    ),
    (
        SourceRetriesExhaustedError,
        FailureCategory.TRANSIENT,
        False,
        "source_retries_exhausted",
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
    """Validate provider-independent state transitions and replay boundaries.

    A connector-free reindex starts from an already authoritative immutable canonical
    artifact, so ``PENDING`` may go straight to ``CANONICAL_READY`` without a raw-capture stage.
    """

    def allows(self, current: DocumentVersionState, target: DocumentVersionState) -> bool:
        """Return whether ``current`` may move to ``target``; staying in place is allowed."""
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
        """Raise unless ``current`` may move to ``target``.

        Raises:
            PublicationConflictError: The transition is not allowed.
        """
        if not self.allows(current, target):
            raise PublicationConflictError(
                f"invalid document-version transition: {current.value} -> {target.value}"
            )


class PublicationPolicy:
    """Decide whether a candidate may enter the atomic publication transaction.

    A retried publish whose first attempt committed (the version is active and current) passes
    silently: the publisher answers it as an idempotent replay, so it is not a conflict.
    """

    def requires_publication(self, decision: SourceAdmissionDecision) -> bool:
        """Return whether an admission decision leads to a published document version."""
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
        """Raise unless the document version may be published.

        Args:
            decision: Admission decision taken for the candidate.
            state: Current state of the document version.
            requires_processing: Whether the candidate went through the processing stages.
            is_current_active: Whether the version is already the active one.

        Raises:
            PublicationConflictError: The decision cannot publish, or the state is not one
                publication accepts.
        """
        if not self.requires_publication(decision):
            raise PublicationConflictError(
                f"admission decision {decision.value} cannot publish a document version"
            )
        if state == DocumentVersionState.ACTIVE and is_current_active:
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
        """Return whether ``document_version_id`` is not the active version and may be cleaned up."""
        return active_version_id is None or active_version_id != document_version_id


@dataclass(frozen=True, slots=True)
class SafeFailure:
    """A failure reduced to what may be stored and shown.

    Args:
        category: Failure category.
        retryable: Whether the run may retry.
        code: Safe error code, free of document content.
    """

    category: FailureCategory
    retryable: bool
    code: str


class IngestionFailureClassifier:
    """Map normalized domain errors to safe durable failure information.

    Infrastructure failures -- a dropped connection, a timeout, a full or
    unavailable object store -- are transient in every stage. Adapter-level
    types core cannot import (storage, S3, SQL drivers) are supplied by the runtime.
    """

    def __init__(
        self,
        transient_errors: tuple[type[BaseException], ...] = (),
        *,
        known_failures: tuple[KnownFailure, ...] = (),
    ) -> None:
        """Build a classifier.

        Args:
            transient_errors: Extra infrastructure error types, transient in every stage.
            known_failures: Extra error types with a fixed classification, matched first.
        """
        self._transient_errors = (*_TRANSIENT_ERRORS, *transient_errors)
        self._known_failures = (*known_failures, *_SIMPLE_FAILURES)

    def classify(self, stage: str, error: Exception) -> SafeFailure:
        """Return the safe category, retry flag and code for ``error`` raised in ``stage``."""
        for error_type, category, retryable, code in self._known_failures:
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
