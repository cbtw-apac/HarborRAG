from __future__ import annotations

import logging

import pytest

from harborrag_core.observability.log_events import (
    MAX_VALUE_LENGTH,
    FailureKind,
    failure_fields,
    is_safe_field_name,
    log_context,
    log_event,
    log_failure,
)

pytestmark = pytest.mark.unit

LOGGER = logging.getLogger("harborrag.test.log_events")


class _SourceRejected(Exception):
    """A connector error that carries its HTTP status, like a vendor 401."""

    status_code = 401
    retryable = False


@pytest.fixture
def capture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    return caplog


def test_an_event_puts_its_fields_in_the_message_and_on_the_record(
    capture: pytest.LogCaptureFixture,
) -> None:
    log_event(
        LOGGER,
        "ingestion.discovery_completed",
        task_id="task-1",
        documents_count=12,
        reason="two words",
    )

    (record,) = capture.records
    assert (
        record.getMessage()
        == 'ingestion.discovery_completed task_id=task-1 documents_count=12 reason="two words"'
    )
    assert record.__dict__["event"] == "ingestion.discovery_completed"
    assert record.__dict__["documents_count"] == 12
    assert record.funcName == "test_an_event_puts_its_fields_in_the_message_and_on_the_record"


def test_unsafe_field_names_and_values_are_dropped_and_named(
    capture: pytest.LogCaptureFixture,
) -> None:
    log_event(
        LOGGER,
        "connector.page_read",
        page_id="p-1",
        title="Alex Example CV",
        labels=["a"],
        item_id=["x"],
    )

    (record,) = capture.records
    assert "Alex Example" not in record.getMessage()
    assert record.__dict__["dropped_fields"] == "item_id,labels,title"
    assert "title" not in record.__dict__


def test_long_values_are_cut_to_the_maximum_length(capture: pytest.LogCaptureFixture) -> None:
    log_event(LOGGER, "test.long", reason="x" * (MAX_VALUE_LENGTH + 50))

    assert len(capture.records[0].__dict__["reason"]) == MAX_VALUE_LENGTH


def test_context_fields_apply_inside_the_block_and_an_event_field_wins(
    capture: pytest.LogCaptureFixture,
) -> None:
    with log_context(task_id="task-1", connector="jira"), log_context(connector="github"):
        log_event(LOGGER, "test.inner", stage="fetch", task_id="task-2")
    log_event(LOGGER, "test.outer")

    inner, outer = capture.records
    assert (inner.__dict__["task_id"], inner.__dict__["connector"]) == ("task-2", "github")
    assert "task_id" not in outer.__dict__


def test_a_failure_names_the_root_cause_behind_a_suppressed_wrapper(
    capture: pytest.LogCaptureFixture,
) -> None:
    def wrapped() -> None:
        try:
            raise _SourceRejected("token for alex@example.com expired")
        except _SourceRejected as cause:
            raise RuntimeError("activity failed") from cause

    with pytest.raises(RuntimeError) as caught:
        wrapped()
    log_failure(
        LOGGER,
        "ingestion.activity_failed",
        caught.value,
        error_code="authentication_failed",
        stage="discovery",
    )

    (record,) = capture.records
    message = record.getMessage()
    assert "alex@example.com" not in message
    assert "activity failed" not in message
    assert record.__dict__["error_type"] == "_SourceRejected"
    assert record.__dict__["error_chain"] == "RuntimeError>_SourceRejected"
    assert (
        record.__dict__["error_code"],
        record.__dict__["status_code"],
        record.__dict__["retryable"],
    ) == (
        "authentication_failed",
        401,
        False,
    )
    assert record.exc_info is not None


def test_a_classified_failure_is_an_error_without_a_traceback(
    capture: pytest.LogCaptureFixture,
) -> None:
    log_failure(
        LOGGER, "connector.request_failed", PermissionError("denied"), FailureKind.CLASSIFIED
    )

    (record,) = capture.records
    assert (record.levelno, record.exc_info) == (logging.ERROR, None)


def test_an_expected_failure_carries_no_traceback(capture: pytest.LogCaptureFixture) -> None:
    log_failure(LOGGER, "mcp.tool_failed", LookupError("no index"), FailureKind.EXPECTED)

    assert capture.records[0].exc_info is None
    assert capture.records[0].levelno == logging.WARNING
    assert capture.records[0].__dict__["error_type"] == "LookupError"


def test_failure_fields_ignore_attributes_of_the_wrong_type() -> None:
    error = ValueError("bad")
    error.status_code = "401"  # type: ignore[attr-defined]  # a vendor exception with a string status
    error.retryable = 1  # type: ignore[attr-defined]  # truthy but not a bool

    assert failure_fields(error) == {"error_type": "ValueError"}


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("task_id", True),
        ("documents_count", True),
        ("error_code", True),
        ("skip_reason", True),
        ("stage", True),
        ("title", False),
        ("file_name", False),
        ("query", False),
        ("config_path", True),
        ("server_port", True),
        ("server_url", False),
        ("document_path", False),
        ("msg", False),
        ("Task_id", False),
    ],
)
def test_only_id_count_code_reason_and_duration_names_are_safe(name: str, safe: bool) -> None:
    assert is_safe_field_name(name) is safe


def test_a_hint_ends_the_message_but_is_not_a_record_field(
    capture: pytest.LogCaptureFixture,
) -> None:
    log_event(
        LOGGER,
        "config.dev_secrets_key",
        level=logging.WARNING,
        backend="sqlite",
        hint="Set HARBORRAG_SECRETS_ENCRYPTION_KEY before sharing the database.",
    )

    (record,) = capture.records
    assert record.getMessage() == (
        "config.dev_secrets_key backend=sqlite "
        'hint="Set HARBORRAG_SECRETS_ENCRYPTION_KEY before sharing the database."'
    )
    assert "hint" not in record.__dict__


def test_a_logger_adapter_keeps_its_own_extra_and_the_caller_location(
    capture: pytest.LogCaptureFixture,
) -> None:
    adapter = logging.LoggerAdapter(LOGGER, {"workflow_id": "wf-1"})

    log_event(adapter, "temporal.schedule_run_started", schedule_id="sched-1")
    log_failure(adapter, "temporal.schedule_run_failed", ValueError("x"), FailureKind.EXPECTED)

    started, failed = capture.records
    assert started.getMessage() == "temporal.schedule_run_started schedule_id=sched-1"
    assert started.__dict__["schedule_id"] == "sched-1"
    assert started.funcName == "test_a_logger_adapter_keeps_its_own_extra_and_the_caller_location"
    assert failed.levelno == logging.WARNING
    assert failed.__dict__["error_type"] == "ValueError"


def test_an_event_about_an_error_carries_its_failure_fields_at_any_level(
    capture: pytest.LogCaptureFixture,
) -> None:
    log_event(
        LOGGER,
        "ingestion.workflow_status_unavailable",
        level=logging.DEBUG,
        error=_SourceRejected("token rejected for alex@example.com"),
        task_id="task-1",
    )

    (record,) = capture.records
    assert record.levelno == logging.DEBUG
    assert record.__dict__["error_type"] == "_SourceRejected"
    assert record.__dict__["status_code"] == 401
    assert record.__dict__["task_id"] == "task-1"
    assert record.exc_info is None
    assert "alex@example.com" not in record.getMessage()
