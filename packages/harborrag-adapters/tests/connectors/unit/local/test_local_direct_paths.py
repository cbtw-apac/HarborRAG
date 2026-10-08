"""Direct local paths obey discovery's selection policy; FIFOs fail fast."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_load_by_paths_refuses_hidden_files(tmp_path: Path):
    hidden = write_file(tmp_path / ".secrets" / "token.txt", "s3cr3t")
    connector = LocalFileConnector(config(tmp_path))

    with pytest.raises(DocumentProcessingError, match="excluded by connector policy"):
        list(connector.load_by_paths([hidden]))


def test_load_by_paths_refuses_disallowed_extensions(tmp_path: Path):
    path = write_file(tmp_path / "tool.exe", b"MZ")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    with pytest.raises(DocumentProcessingError, match="excluded by connector policy"):
        list(connector.load_by_paths([path]))


def test_load_by_paths_still_loads_selected_files(tmp_path: Path):
    path = write_file(tmp_path / "docs" / "README.md", b"# ok")
    connector = LocalFileConnector(config(tmp_path))

    assert [document.content for document in connector.load_by_paths([path])] == [b"# ok"]


def _error_within_timeout(fifo: Path, action: Callable[[], object]) -> BaseException | None:
    """Run ``action`` in a thread; fail (instead of hanging) if it blocks on ``fifo``."""

    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # surfaced to the test thread below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        with open(fifo, "wb"):
            pass  # give the blocked reader a writer so the thread can finish
        worker.join(timeout=5)
        pytest.fail("reading a FIFO blocked waiting for a writer")
    return outcome.get("error")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX mkfifo")
def test_load_by_paths_refuses_a_fifo_without_opening_it(tmp_path: Path):
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    connector = LocalFileConnector(config(tmp_path, checksum_mode="sha256"))

    error = _error_within_timeout(fifo, lambda: list(connector.load_by_paths([fifo])))

    assert isinstance(error, DocumentProcessingError)
    assert "excluded by connector policy" in str(error)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX mkfifo")
def test_loading_a_fifo_record_fails_fast_instead_of_blocking(tmp_path: Path):
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    connector = LocalFileConnector(config(tmp_path))
    record = SourceRecord(
        id="pipe.txt", source_type="text/plain", locator=str(fifo), metadata={"path": str(fifo)}
    )

    error = _error_within_timeout(fifo, lambda: connector.load(record))

    assert isinstance(error, DocumentProcessingError)
    assert "not a regular file" in str(error)
