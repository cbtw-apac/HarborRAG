"""Contract tests for the public API request/response schemas.

These schemas are not yet wired into a route, so this suite is their only
consumer. It pins the invariants a future transport must be able to rely on:
strict rejection of unknown fields, the declared field bounds, and the
task-state consistency rules encoded in the model validators.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_app.api.schemas import (
    IngestInput,
    IngestResult,
    IngestSourceInput,
    RetrievedItem,
    RetrieveInput,
    RetrieveResult,
    TaskError,
    TaskOperation,
    TaskProgress,
    TaskStatus,
)

# --------------------------------------------------------------------------
# Shared base config
# --------------------------------------------------------------------------


def test_unknown_fields_are_rejected() -> None:
    """extra="forbid" must reject typos rather than silently dropping them."""
    with pytest.raises(ValidationError) as excinfo:
        TaskError(code="e", message="m", retryable_typo=True)  # type: ignore[call-arg]
    assert "retryable_typo" in str(excinfo.value)


def test_strings_are_stripped() -> None:
    """str_strip_whitespace normalises padded input at the boundary."""
    error = TaskError(code="  boom  ", message="  went wrong  ")
    assert error.code == "boom"
    assert error.message == "went wrong"


def test_assignment_is_validated() -> None:
    """validate_assignment keeps a model valid after construction."""
    error = TaskError(code="boom", message="went wrong")
    with pytest.raises(ValidationError):
        error.code = ""


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


def test_status_and_operation_are_string_enums() -> None:
    """StrEnum members compare equal to their wire values."""
    assert TaskStatus.SUCCEEDED == "succeeded"
    assert TaskOperation.INGEST == "ingest"
    assert TaskStatus("pending") is TaskStatus.PENDING
    assert {s.value for s in TaskStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }


# --------------------------------------------------------------------------
# TaskError / TaskProgress
# --------------------------------------------------------------------------


def test_task_error_defaults_and_bounds() -> None:
    error = TaskError(code="c", message="m")
    assert error.retryable is False
    assert error.details == {}

    with pytest.raises(ValidationError):
        TaskError(code="", message="m")
    with pytest.raises(ValidationError):
        TaskError(code="c" * 101, message="m")
    with pytest.raises(ValidationError):
        TaskError(code="c", message="m" * 2_001)


def test_task_progress_accepts_consistent_counts() -> None:
    assert TaskProgress(stage="index").completed == 0
    assert TaskProgress(stage="index", completed=2, total=5).total == 5
    # An unbounded stage is allowed: total stays None.
    assert TaskProgress(stage="index", completed=99).total is None
    # Equality is the boundary case and must be accepted.
    assert TaskProgress(stage="index", completed=5, total=5).completed == 5


def test_task_progress_rejects_overshoot_and_negatives() -> None:
    with pytest.raises(ValidationError, match="completed cannot be greater than total"):
        TaskProgress(stage="index", completed=6, total=5)
    with pytest.raises(ValidationError):
        TaskProgress(stage="index", completed=-1)
    with pytest.raises(ValidationError):
        TaskProgress(stage="", completed=0)


# --------------------------------------------------------------------------
# Ingest / retrieve payloads
# --------------------------------------------------------------------------


def test_ingest_input_defaults() -> None:
    payload = IngestInput(source=IngestSourceInput(connector="local_file"))
    assert payload.namespace == "default"
    assert payload.pipeline == "default"
    assert payload.idempotency_key is None
    assert payload.metadata == {}
    assert payload.source.reference is None
    assert payload.source.parameters == {}


def test_ingest_input_bounds() -> None:
    with pytest.raises(ValidationError):
        IngestSourceInput(connector="")
    with pytest.raises(ValidationError):
        IngestSourceInput(connector="local_file", reference="r" * 4_097)
    with pytest.raises(ValidationError):
        IngestInput(source=IngestSourceInput(connector="c"), namespace="")
    with pytest.raises(ValidationError):
        IngestInput(source=IngestSourceInput(connector="c"), idempotency_key="")


def test_ingest_result_defaults_and_non_negative() -> None:
    result = IngestResult()
    assert result.documents_discovered == 0
    assert result.chunks_indexed == 0
    assert result.checkpoint_id is None
    assert result.warnings == []
    with pytest.raises(ValidationError):
        IngestResult(documents_failed=-1)


def test_retrieve_input_defaults_and_bounds() -> None:
    payload = RetrieveInput(query="what is harborrag")
    assert payload.top_k == 10
    assert payload.namespace == "default"
    assert payload.retrieval_profile == "default"
    assert payload.include_content is True
    assert payload.include_metadata is True
    assert payload.filters == {}

    with pytest.raises(ValidationError):
        RetrieveInput(query="")
    with pytest.raises(ValidationError):
        RetrieveInput(query="q", top_k=0)
    with pytest.raises(ValidationError):
        RetrieveInput(query="q", top_k=1_001)


def test_retrieve_result_shape() -> None:
    item = RetrievedItem(chunk_id="c1", document_id="d1", score=0.5)
    assert item.content is None
    assert item.metadata == {}

    result = RetrieveResult(items=[item], retrieval_profile="default", duration_ms=12.5)
    assert result.items[0].chunk_id == "c1"

    with pytest.raises(ValidationError):
        RetrieveResult(retrieval_profile="default", duration_ms=-1)
    with pytest.raises(ValidationError):
        RetrievedItem(chunk_id="", document_id="d1", score=0.5)
