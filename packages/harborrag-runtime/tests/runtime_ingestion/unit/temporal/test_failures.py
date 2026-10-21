"""Safe ingestion failure classification and Temporal retry intent."""

from __future__ import annotations

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    TerminatedError,
    TimeoutError,
)

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
)
from harborrag_adapters.parsers.errors import ParseError
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion import FailureCategory
from harborrag_engine.ingestion import IngestionFailureClassifier
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.temporal.activity_observability import (
    ActivityObservability,
)
from harborrag_runtime.temporal.failure_handling import durable_failure, terminal_failure_code

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("stage", "error", "retryable"),
    [
        ("DiscoverSourceItems", AuthenticationError("denied"), False),
        ("DiscoverSourceItems", AuthorizationError("insufficient scope"), False),
        ("FetchAndCaptureRaw", AuthenticationError("denied"), False),
        ("FetchAndCaptureRaw", AuthorizationError("insufficient scope"), False),
        ("FetchAndCaptureRaw", FetchError("temporary"), True),
        ("ParseAndNormalize", ParseError("invalid source"), False),
        ("ChunkAndValidate", ValueError("invalid chunk"), False),
        ("BuildRelations", HarborConflictError("immutable mismatch"), False),
        ("WriteVectorProjection", RuntimeError("unavailable"), True),
    ],
)
def test_failure_classifier_matches_activity_retry_matrix(
    stage: str,
    error: Exception,
    retryable: bool,
) -> None:
    failure = IngestionFailureClassifier().classify(stage, error)

    assert failure.retryable is retryable


def test_authentication_and_authorization_errors_get_distinct_non_retryable_codes() -> None:
    """401 (invalid credentials) and 403 (valid credentials, insufficient
    scope) must not collapse into the same generic `source_forbidden` code --
    the recorded failure reason has to let an operator tell them apart, and
    both must stay non-retryable. This also locks in `_SIMPLE_FAILURES`
    ordering: both are `SourceForbiddenError` subclasses, so a specific entry
    placed after the generic one would silently mask this distinction."""
    classifier = IngestionFailureClassifier()

    authentication = classifier.classify("DiscoverSourceItems", AuthenticationError("denied"))
    authorization = classifier.classify(
        "DiscoverSourceItems",
        AuthorizationError("insufficient scope"),
    )

    assert authentication.code == "authentication_failed"
    assert authentication.retryable is False
    assert authorization.code == "authorization_failed"
    assert authorization.retryable is False
    assert authentication.code != authorization.code


def test_immutable_artifact_conflict_is_a_safe_validation_failure() -> None:
    failure = IngestionFailureClassifier().classify(
        "BuildRelations",
        HarborConflictError("private artifact key"),
    )

    assert failure.category == FailureCategory.CANONICAL_VALIDATION
    assert failure.code == "immutable_artifact_conflict"
    assert failure.retryable is False


def test_activity_boundary_emits_safe_non_retryable_application_error() -> None:
    observability = ActivityObservability(
        IngestionTelemetry(),
        failures=IngestionFailureClassifier(),
    )

    with pytest.raises(ApplicationError) as captured:
        with observability.boundary("ChunkAndValidate"):
            raise ValueError("source content that must not enter history")

    assert captured.value.non_retryable is True
    assert captured.value.type == "chunkandvalidate_valueerror"
    assert "source content" not in str(captured.value)


def test_durable_failure_uses_declared_safe_application_error_type() -> None:
    error = ApplicationError(
        "safe operator message",
        type="parser_format_unsupported",
        non_retryable=True,
    )

    failure_type, message = durable_failure(error)

    assert failure_type == "parser_format_unsupported"
    assert "inspect restricted worker logs" in message


class _FakeActivityError(Exception):
    """Stands in for Temporal's `ActivityError`, whose `.cause` at the
    workflow level is the activity's own `ApplicationError` -- the one level
    `durable_failure` actually unwraps in production (`source_workflow.py`'s
    `except (ActivityError, ChildWorkflowError) as error: durable_failure(error)`)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("activity failed")
        self.cause = cause


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_status"),
    [
        (AuthenticationError("bad token", status_code=401), "authentication_failed", 401),
        (
            AuthorizationError("insufficient scope", status_code=403),
            "authorization_failed",
            403,
        ),
    ],
)
def test_activity_boundary_emits_distinct_non_retryable_code_for_401_and_403(
    error: Exception,
    expected_code: str,
    expected_status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closes the loop the ticket actually observes: run A's 401 and run B's
    403 must each fail on attempt 1 (`non_retryable=True`) *and* land in
    `record_source_failure`'s `error_code` (via `durable_failure`) as two
    distinct reasons, not the same one. The raw HTTP status must also be
    visible directly in the worker log, not just implied by the error type."""
    observability = ActivityObservability(
        IngestionTelemetry(),
        failures=IngestionFailureClassifier(),
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(ApplicationError) as captured:
            with observability.boundary("DiscoverSourceItems"):
                raise error

    assert captured.value.non_retryable is True
    assert captured.value.type == expected_code
    assert f"status_code={expected_status}" in caplog.text

    failure_type, _ = durable_failure(_FakeActivityError(captured.value))

    assert failure_type == expected_code


def _chain(*errors: BaseException) -> BaseException:
    for outer, inner in zip(errors, errors[1:], strict=False):
        outer.__cause__ = inner
    return errors[0]


def _activity_error() -> ActivityError:
    return ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="x",
        activity_id="1",
        retry_state=None,
    )


def test_terminal_failure_code_unwraps_child_workflow_chains() -> None:
    child = ChildWorkflowError(
        "child failed",
        namespace="default",
        workflow_id="wf",
        run_id="run",
        workflow_type="harborrag.source_batch",
        initiated_event_id=1,
        started_event_id=2,
        retry_state=None,
    )
    error = _chain(
        WorkflowFailureError(cause=Exception("x")),
        child,
        _activity_error(),
        ApplicationError("hunter2", type="source_unavailable"),
    )

    assert terminal_failure_code(error) == "source_unavailable"


def test_terminal_failure_code_handles_terminal_causes() -> None:
    def wrapped(cause: BaseException) -> BaseException:
        return _chain(WorkflowFailureError(cause=cause), cause)

    assert (
        terminal_failure_code(wrapped(TimeoutError("t", type=None, last_heartbeat_details=[])))
        == "timeout"
    )
    assert terminal_failure_code(wrapped(TerminatedError("t", []))) == "terminated"
    assert terminal_failure_code(wrapped(CancelledError("c"))) == "cancelled"
    assert terminal_failure_code(WorkflowFailureError(cause=Exception("x"))) == "unknown"
    assert terminal_failure_code(RuntimeError("hunter2")) == "unknown"
