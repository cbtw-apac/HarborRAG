"""Local listing failures, FIFO reads and path-free logs and errors."""

from __future__ import annotations

import errno
import logging
import os
import threading
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import (
    ConnectorError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.local.skips import SKIP_REASON_DELETED, path_digest
from harborrag_core.ingestion.connector_contracts import SKIP_REASON_UNREADABLE

pytestmark = pytest.mark.unit

FIFO_WAIT_SECONDS = 5.0


def _fail_listing(directory: Path, error: OSError, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = os.path.realpath(directory)
    original_iterdir = Path.iterdir

    def iterdir(self: Path):
        if os.path.realpath(self) == blocked:
            raise error
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_listing_failure_that_is_not_denied_or_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "plans" / "quarterly-plan.md")
    _fail_listing(tmp_path / "plans", OSError(errno.EIO, "Input/output error"), monkeypatch)
    connector = LocalFileConnector(config(tmp_path))

    with pytest.raises(FetchError, match="Could not list local directory") as raised:
        list(connector.discover())

    assert "plans" not in str(raised.value)
    assert connector.skipped == ()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PermissionError(errno.EACCES, "Permission denied"), SKIP_REASON_UNREADABLE),
        (PermissionError(errno.EPERM, "Operation not permitted"), SKIP_REASON_UNREADABLE),
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), SKIP_REASON_DELETED),
    ],
)
def test_denied_or_deleted_directory_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, reason: str
) -> None:
    write_file(tmp_path / "plans" / "quarterly-plan.md")
    write_file(tmp_path / "roadmap.md")
    _fail_listing(tmp_path / "plans", error, monkeypatch)
    connector = LocalFileConnector(config(tmp_path))

    records = list(connector.discover())

    assert [record.metadata["relative_path"] for record in records] == ["roadmap.md"]
    assert [skip.reason for skip in connector.skipped] == [reason]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_load_refuses_a_fifo_swapped_in_after_discovery(tmp_path: Path) -> None:
    path = write_file(tmp_path / "quarterly-plan.md")
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
    assert len(outcome) == 1
    assert isinstance(outcome[0], DocumentProcessingError)
    assert "quarterly-plan" not in str(outcome[0])


def test_load_logs_and_errors_name_the_file_by_digest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_file(tmp_path / "quarterly-plan.md", "plan")
    connector = LocalFileConnector(config(tmp_path))
    record = next(iter(connector.discover()))

    with caplog.at_level(logging.INFO, logger="harborrag.adapters.connectors.local"):
        connector.load(record)
    path.unlink()
    with pytest.raises(FetchError) as raised:
        connector.load(record)

    assert f"path_digest={path_digest(path)}" in caplog.text
    assert "quarterly-plan" not in caplog.text
    assert "quarterly-plan" not in str(raised.value)
    assert raised.value.__cause__ is None
