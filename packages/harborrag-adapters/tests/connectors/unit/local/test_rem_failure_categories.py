"""Connector errors classify into a category that agrees with their retry flag."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import (
    ConnectorError,
    FetchError,
    NotRegularFileError,
    RefusedResponseError,
)
from harborrag_core.ingestion import FailureCategory, IngestionFailureClassifier

pytestmark = pytest.mark.unit

FIFO_WAIT_SECONDS = 5.0


@pytest.mark.parametrize(
    "error",
    [
        FetchError("bad JQL", status_code=400),
        FetchError("gone", status_code=410),
        FetchError("unprocessable", status_code=422),
        RefusedResponseError("redirect to an untrusted origin"),
    ],
    ids=["400", "410", "422", "refused"],
)
def test_permanent_source_answer_is_invalid_configuration(error: FetchError) -> None:
    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", error)

    assert failure.category is FailureCategory.INVALID_CONFIGURATION
    assert failure.retryable is False
    assert failure.code == "source_unavailable"


def test_server_error_stays_transient() -> None:
    error = FetchError("unavailable", status_code=503)

    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", error)

    assert failure.category is FailureCategory.TRANSIENT
    assert failure.retryable is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_fifo_swapped_in_after_discovery_is_an_unsupported_document(tmp_path: Path) -> None:
    path = write_file(tmp_path / "demo-design-notes.md")
    connector = LocalFileConnector(config(tmp_path))
    record = next(iter(connector.discover()))
    path.unlink()
    os.mkfifo(path)
    outcome: list[BaseException | object] = []

    def load() -> None:
        try:
            outcome.append(connector.load(record))
        except ConnectorError as error:
            outcome.append(error)

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    worker.join(FIFO_WAIT_SECONDS)

    assert not worker.is_alive()
    [error] = outcome
    assert isinstance(error, NotRegularFileError)
    failure = IngestionFailureClassifier().classify("FetchAndCaptureRaw", error)
    assert failure.category is FailureCategory.UNSUPPORTED
    assert failure.retryable is False
    assert failure.code == "document_unsupported"
