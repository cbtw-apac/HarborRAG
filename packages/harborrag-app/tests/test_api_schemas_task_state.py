"""Task-state invariants for the public API fetch/output schemas.

Split from ``test_api_schemas.py`` to stay under the repository file-length
gate. This half covers the ``FetchOutput`` model validator and the two
concrete operation outputs; the other half covers the request payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harborrag_app.api.schemas import (
    FetchOutput,
    IngestOutput,
    IngestResult,
    RetrievedItem,
    RetrieveOutput,
    RetrieveResult,
    TaskError,
    TaskOperation,
    TaskProgress,
    TaskStatus,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


def _times(**overrides: object) -> dict[str, object]:
    """Baseline timestamp block for a FetchOutput, overridable per case."""
    return {"created_at": NOW, "updated_at": LATER, **overrides}


def _ingest_result() -> IngestResult:
    return IngestResult(documents_discovered=3, documents_processed=3)


# --------------------------------------------------------------------------
# FetchOutput task-state invariants
# --------------------------------------------------------------------------


def test_succeeded_task_requires_result_and_no_error() -> None:
    output = FetchOutput[IngestResult](
        task_id="t1",
        operation=TaskOperation.INGEST,
        status=TaskStatus.SUCCEEDED,
        result=_ingest_result(),
        completed_at=LATER,
        **_times(),
    )
    assert output.result is not None
    assert output.error is None

    with pytest.raises(ValidationError, match="result is required when status is succeeded"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.SUCCEEDED,
            completed_at=LATER,
            **_times(),
        )

    with pytest.raises(ValidationError, match="error must be empty when status is succeeded"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.SUCCEEDED,
            result=_ingest_result(),
            error=TaskError(code="c", message="m"),
            completed_at=LATER,
            **_times(),
        )


def test_result_is_rejected_for_non_succeeded_status() -> None:
    with pytest.raises(
        ValidationError, match="result may only be populated when status is succeeded"
    ):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.RUNNING,
            result=_ingest_result(),
            **_times(),
        )


def test_failed_task_requires_an_error() -> None:
    with pytest.raises(ValidationError, match="error is required when status is failed"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.FAILED,
            completed_at=LATER,
            **_times(),
        )

    output = FetchOutput[IngestResult](
        task_id="t1",
        operation=TaskOperation.INGEST,
        status=TaskStatus.FAILED,
        error=TaskError(code="c", message="m"),
        completed_at=LATER,
        **_times(),
    )
    assert output.error is not None


@pytest.mark.parametrize(
    "status",
    [TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
def test_terminal_status_requires_completed_at(status: TaskStatus) -> None:
    extras: dict[str, object] = {}
    if status is TaskStatus.SUCCEEDED:
        extras["result"] = _ingest_result()
    if status is TaskStatus.FAILED:
        extras["error"] = TaskError(code="c", message="m")

    with pytest.raises(
        ValidationError, match="completed_at is required for terminal task statuses"
    ):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=status,
            **extras,
            **_times(),
        )


@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.RUNNING])
def test_non_terminal_status_forbids_completed_at(status: TaskStatus) -> None:
    with pytest.raises(
        ValidationError, match="completed_at must be empty for non-terminal task statuses"
    ):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=status,
            completed_at=LATER,
            **_times(),
        )


def test_timestamps_must_not_precede_creation() -> None:
    earlier = NOW - timedelta(minutes=1)

    with pytest.raises(ValidationError, match="updated_at cannot be earlier than created_at"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.RUNNING,
            created_at=NOW,
            updated_at=earlier,
        )

    with pytest.raises(ValidationError, match="started_at cannot be earlier than created_at"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.RUNNING,
            started_at=earlier,
            **_times(),
        )

    with pytest.raises(ValidationError, match="completed_at cannot be earlier than created_at"):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.CANCELLED,
            completed_at=earlier,
            **_times(),
        )


def test_naive_datetimes_are_rejected() -> None:
    """AwareDatetime keeps timezone-naive input out of the public contract."""
    with pytest.raises(ValidationError):
        FetchOutput[IngestResult](
            task_id="t1",
            operation=TaskOperation.INGEST,
            status=TaskStatus.RUNNING,
            created_at=datetime(2026, 7, 25, 12, 0),  # noqa: DTZ001
            updated_at=LATER,
        )


def test_started_at_may_equal_created_at() -> None:
    output = FetchOutput[IngestResult](
        task_id="t1",
        operation=TaskOperation.INGEST,
        status=TaskStatus.RUNNING,
        started_at=NOW,
        **_times(),
    )
    assert output.started_at == NOW
    assert output.progress is None
    assert output.message is None


# --------------------------------------------------------------------------
# Concrete operation outputs
# --------------------------------------------------------------------------


def test_ingest_output_pins_its_operation() -> None:
    output = IngestOutput(
        task_id="t1",
        status=TaskStatus.SUCCEEDED,
        result=_ingest_result(),
        progress=TaskProgress(stage="index", completed=3, total=3),
        completed_at=LATER,
        **_times(),
    )
    assert output.operation is TaskOperation.INGEST
    assert output.result is not None
    assert output.result.documents_processed == 3

    with pytest.raises(ValidationError):
        IngestOutput(
            task_id="t1",
            operation=TaskOperation.RETRIEVE,
            status=TaskStatus.RUNNING,
            **_times(),
        )


def test_retrieve_output_pins_its_operation() -> None:
    output = RetrieveOutput(
        task_id="t2",
        status=TaskStatus.SUCCEEDED,
        result=RetrieveResult(
            items=[RetrievedItem(chunk_id="c1", document_id="d1", score=0.9)],
            retrieval_profile="default",
            duration_ms=4.0,
        ),
        completed_at=LATER,
        **_times(),
    )
    assert output.operation is TaskOperation.RETRIEVE
    assert output.result is not None
    assert output.result.items[0].score == pytest.approx(0.9)


def test_outputs_round_trip_through_json() -> None:
    """The contract must survive serialisation, which is how a route uses it."""
    output = IngestOutput(
        task_id="t1",
        status=TaskStatus.SUCCEEDED,
        result=_ingest_result(),
        completed_at=LATER,
        **_times(),
    )
    restored = IngestOutput.model_validate_json(output.model_dump_json())
    assert restored == output
