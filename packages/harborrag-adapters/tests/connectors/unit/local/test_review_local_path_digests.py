"""Local discovery names files by digest in logs and errors, never by path (CON-1)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem import LocalFileSystem
from harborrag_adapters.connectors.local.selection import LocalFileSelector
from harborrag_adapters.connectors.local.skips import LocalSkipReport, path_digest
from harborrag_core.ingestion.connector_contracts import ConnectorQuery

pytestmark = pytest.mark.unit

PERSON = "Alex_Example"


def test_discovery_logs_its_start_path_by_digest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source_root = tmp_path / f"{PERSON}_CVs"
    write_file(source_root / "notes.md")

    with caplog.at_level(logging.DEBUG, logger="harborrag.adapters.connectors.local"):
        list(LocalFileConnector(config(source_root)).discover(ConnectorQuery()))

    assert f"path_digest={path_digest(source_root)}" in caplog.text
    assert PERSON not in caplog.text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_a_path_that_is_neither_file_nor_directory_is_named_by_digest(tmp_path: Path) -> None:
    pipe = tmp_path / f"{PERSON}_CV.pdf"
    os.mkfifo(pipe)

    with pytest.raises(DocumentProcessingError) as raised:
        list(LocalFileConnector(config(tmp_path)).discover(ConnectorQuery(path=str(pipe))))

    assert f"path_digest={path_digest(pipe)}" in str(raised.value)
    assert PERSON not in str(raised.value)


def test_a_disabled_symlink_is_named_by_digest(tmp_path: Path) -> None:
    target = write_file(tmp_path / f"{PERSON}_CV.md")
    link = tmp_path / f"{PERSON}_link.md"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    files = LocalFileSystem(config(tmp_path, follow_symlinks=False))

    with pytest.raises(DocumentProcessingError) as raised:
        files.resolve_candidate(link)

    assert f"path_digest={path_digest(link)}" in str(raised.value)
    assert PERSON not in str(raised.value)


def test_a_file_that_cannot_be_read_for_its_size_is_named_by_digest(tmp_path: Path) -> None:
    missing = tmp_path / f"{PERSON}_CV.md"
    selector = LocalFileSelector(
        config(tmp_path, fail_on_error=True),
        tmp_path,
        LocalSkipReport(),
        within_source_scope=lambda path: True,
        has_symlink_component=lambda path: False,
    )

    with pytest.raises(FetchError) as raised:
        selector._candidate_stat(missing)

    assert str(raised.value) == (
        f"Could not stat local file path_digest={path_digest(missing)}: FileNotFoundError"
    )
    assert raised.value.__cause__ is None
