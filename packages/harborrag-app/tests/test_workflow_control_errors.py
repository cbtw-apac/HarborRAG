"""Public error text never carries submitted values; failures stay visible in logs."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from harborrag_app.workflow_control.errors import failure_response, public_error_message

_SECRET = "s3cr3t-token-value"


class _Settings(BaseModel):
    port: int
    endpoint: str

    @field_validator("endpoint")
    @classmethod
    def _no_plaintext(cls, value: str) -> str:
        raise ValueError(f"endpoint {value!r} must use https")


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _Settings.model_validate({"port": _SECRET, "endpoint": f"http://{_SECRET}"})
    return caught.value


def test_a_pydantic_error_names_fields_but_never_their_values() -> None:
    message = public_error_message(_validation_error())

    assert _SECRET not in message
    assert "input_value" not in message
    assert message == "Invalid input for field(s): port, endpoint"


def test_a_plain_value_error_keeps_its_reviewed_message() -> None:
    assert (
        public_error_message(ValueError("run_id must not be empty")) == "run_id must not be empty"
    )


def test_an_unreviewed_error_collapses_to_its_class_name() -> None:
    assert public_error_message(RuntimeError(f"upstream said {_SECRET}")) == "RuntimeError"


def test_a_reviewed_failure_is_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("tests.workflow_control.errors")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        response = failure_response(logger, ValueError("bad run id"), "start run %r", "r-1")

    assert response.error == "bad run id"
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.levelno for record in loud] == [logging.WARNING]


def test_a_validation_failure_never_logs_the_secret_at_warning_or_above(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("tests.workflow_control.errors.secret")
    handler_format = logging.Formatter()

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        response = failure_response(logger, _validation_error(), "start run %r", "r-1")

    assert _SECRET not in (response.error or "")
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert loud
    for record in loud:
        assert _SECRET not in record.getMessage()
        assert _SECRET not in (record.exc_text or "")
        assert record.exc_info is None
        assert _SECRET not in handler_format.format(record)
