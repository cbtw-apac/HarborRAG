"""Temporal workflow payloads reject malformed values before they reach history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from harborrag_runtime.ingestion.limits import MAX_RETRY_DOCUMENT_IDS
from harborrag_runtime.temporal.dispatch import DocumentDispatchSummary
from harborrag_runtime.temporal.schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    RetryFailuresInput,
    RetryTaskFailureInput,
    SourceBatchStatus,
    SourceCancellationInput,
    SourceContinuation,
    SourceFailureInput,
    SourceIngestionResult,
    SourceIngestionStatus,
    SourcePauseInput,
    SourceResumeInput,
    WorkflowArtifactReference,
    WorkflowExecutionControlInput,
)

_SHA = "a" * 64
_ARTIFACT = WorkflowArtifactReference(
    bucket="bucket",
    key="plans/one.json",
    sha256=_SHA,
    byte_size=100,
    media_type="application/json",
)
_DOCUMENT = DocumentIngestionInput(
    task_id="task-1",
    tenant_id="tenant-1",
    connector_name="local-docs",
    plan_reference=_ARTIFACT,
    document_index=0,
)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bucket": " "}, "bucket and key must be non-empty"),
        ({"key": ""}, "bucket and key must be non-empty"),
        ({"sha256": "abc"}, "sha256 must be lowercase hexadecimal"),
        ({"sha256": "A" * 64}, "sha256 must be lowercase hexadecimal"),
        ({"media_type": " "}, "media_type must be non-empty"),
        ({"byte_size": -1}, "byte_size must not be negative"),
        ({"byte_offset": 0}, "range values must be set together"),
        ({"byte_length": 4}, "range values must be set together"),
        ({"byte_offset": -1, "byte_length": 4}, "range values must not be negative"),
        ({"byte_offset": 0, "byte_length": -4}, "range values must not be negative"),
        ({"byte_offset": 90, "byte_length": 20}, "range exceeds the artifact size"),
    ],
)
def test_artifact_reference_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_ARTIFACT, **overrides)


def test_artifact_reference_accepts_a_range_inside_the_artifact() -> None:
    ranged = replace(_ARTIFACT, byte_offset=90, byte_length=10)

    assert (ranged.byte_offset, ranged.byte_length) == (90, 10)


def _continuation(**overrides: object) -> SourceContinuation:
    values: dict[str, object] = {
        "scan_id": "scan-1",
        "plan_reference": _ARTIFACT,
        "document_count": 5,
        "next_document_index": 2,
        "batch_number": 1,
        "summary": DocumentDispatchSummary(),
    }
    values.update(overrides)
    return SourceContinuation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"scan_id": " "}, "scan ID must be non-empty"),
        ({"next_document_index": 6}, "document cursor is invalid"),
        ({"next_document_index": -1}, "document cursor is invalid"),
        ({"batch_number": -1}, "counters must not be negative"),
    ],
)
def test_source_continuation_rejects_an_invalid_cursor(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _continuation(**overrides)


def test_source_continuation_allows_a_cursor_at_the_end() -> None:
    assert _continuation(next_document_index=5).next_document_index == 5


def _raw_capture(**overrides: object) -> RawCaptureResult:
    values: dict[str, object] = {
        "document": _DOCUMENT,
        "document_id": "doc-1",
        "document_version_id": None,
        "decision": "CHANGED",
        "connector_type": "local",
        "content_hash": "hash",
        "source_artifact": _ARTIFACT,
        "metadata_artifact": _ARTIFACT,
    }
    values.update(overrides)
    return RawCaptureResult(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"document_id": " "}, "identity must be non-empty"),
        ({"decision": ""}, "identity must be non-empty"),
        ({"content_hash": None}, "reference must be complete"),
        (
            {
                "connector_type": None,
                "content_hash": None,
                "source_artifact": None,
                "metadata_artifact": None,
            },
            "requires an active document version",
        ),
    ],
)
def test_raw_capture_result_rejects_partial_or_unanchored_captures(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _raw_capture(**overrides)


def test_raw_capture_without_bytes_is_accepted_for_an_active_version() -> None:
    capture = _raw_capture(
        document_version_id="version-1",
        connector_type=None,
        content_hash=None,
        source_artifact=None,
        metadata_artifact=None,
    )

    assert capture.document_version_id == "version-1"
    assert _raw_capture().source_artifact == _ARTIFACT


@pytest.mark.parametrize("blank", ["document_id", "document_version_id", "decision"])
def test_prepared_document_requires_every_identity(blank: str) -> None:
    values = {"document_id": "d", "document_version_id": "v", "decision": "CHANGED"}
    values[blank] = " "

    with pytest.raises(ValueError, match="prepared document identity must be non-empty"):
        PreparedDocument(document=_DOCUMENT, **values)


@pytest.mark.parametrize(
    ("stage", "error_type"),
    [(" ", "TimeoutError"), ("parse", "")],
)
def test_document_failure_requires_stage_and_type(stage: str, error_type: str) -> None:
    with pytest.raises(ValueError, match="stage and type must be non-empty"):
        DocumentFailureInput(
            document=_DOCUMENT, prepared=None, failed_stage=stage, error_type=error_type
        )


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: SourceCancellationInput(" "), "cancelled source task ID"),
        (lambda: SourcePauseInput(""), "paused source task ID"),
        (lambda: SourceResumeInput(" "), "resumed source task ID"),
        (lambda: SourceFailureInput(" ", "E"), "task ID and error code"),
        (lambda: SourceFailureInput("task", " "), "task ID and error code"),
        (lambda: WorkflowExecutionControlInput(""), "workflow ID must be non-empty"),
        (lambda: RetryTaskFailureInput("", "E"), "retry task failure ID"),
        (lambda: RetryTaskFailureInput("retry", " "), "retry task failure ID"),
    ],
)
def test_control_inputs_reject_blank_identities(
    factory: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_control_inputs_keep_valid_identities() -> None:
    assert SourceFailureInput("task-1", "E_PARSE").error_code == "E_PARSE"
    assert WorkflowExecutionControlInput("wf-1").workflow_id == "wf-1"
    assert SourceCancellationInput("task-1").task_id == "task-1"
    assert SourcePauseInput("task-2").task_id == "task-2"
    assert SourceResumeInput("task-3").task_id == "task-3"


def test_source_result_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="unsupported status"):
        SourceIngestionResult(
            task_id="t",
            scan_id="s",
            discovered=0,
            published=0,
            unchanged=0,
            failed=0,
            removal_candidates=(),
            unresolved_relations=0,
            status="RUNNING",
        )


@pytest.mark.parametrize(
    ("task_id", "status", "message"),
    [
        (" ", "RUNNING", "source status task ID must be non-empty"),
        ("task-1", "DONE", "source status is invalid"),
    ],
)
def test_source_status_rejects_invalid_values(task_id: str, status: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SourceIngestionStatus(task_id=task_id, status=status, paused=False, cancel_requested=False)


@pytest.mark.parametrize(
    ("task_id", "status", "message"),
    [
        ("", "RUNNING", "batch status task ID must be non-empty"),
        ("task-1", "FAILED", "source batch status is invalid"),
    ],
)
def test_batch_status_rejects_invalid_values(task_id: str, status: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SourceBatchStatus(task_id=task_id, status=status, paused=False, cancel_requested=False)


def test_batch_status_accepts_a_known_state() -> None:
    status = SourceBatchStatus(task_id="t", status="PAUSED", paused=True, cancel_requested=False)

    assert status.paused is True


def _retry(**overrides: object) -> RetryFailuresInput:
    values: dict[str, object] = {
        "retry_task_id": "retry-1",
        "original_task_id": "task-1",
        "tenant_id": "tenant-1",
        "document_ids": ("doc-1", "doc-2"),
    }
    values.update(overrides)
    return RetryFailuresInput(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"retry_task_id": " "}, "retry task identities must be non-empty"),
        ({"tenant_id": ""}, "retry task identities must be non-empty"),
        ({"document_ids": ()}, "document IDs must be non-empty"),
        ({"document_ids": ("doc-1", " ")}, "document IDs must be non-empty"),
        (
            {"document_ids": tuple(f"d{i}" for i in range(MAX_RETRY_DOCUMENT_IDS + 1))},
            f"at most {MAX_RETRY_DOCUMENT_IDS}",
        ),
        ({"document_ids": ("doc-1", "doc-1")}, "document IDs must be unique"),
        ({"document_concurrency": 0}, "between 1 and 100"),
        ({"document_concurrency": 101}, "between 1 and 100"),
    ],
)
def test_retry_input_rejects_invalid_selections(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _retry(**overrides)


def test_retry_input_accepts_a_bounded_unique_selection() -> None:
    retry = _retry(document_concurrency=100)

    assert retry.document_ids == ("doc-1", "doc-2")
    assert retry.document_concurrency == 100
