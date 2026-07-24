"""Unit tests for local-filesystem connector document loading."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem_paths import guess_mime_type
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_load_reads_file_bytes_and_builds_metadata(tmp_path: Path):
    path = write_file(tmp_path / "docs" / "README.md", b"# Hello")
    connector = LocalFileConnector(config(tmp_path, checksum_mode="sha256"))
    record = next(connector.discover(ConnectorQuery(filters={"file_paths": [path]})))

    document = connector.load(record)

    assert document.id == path.resolve().as_uri()
    assert document.source == path.resolve().as_uri()
    assert document.content == b"# Hello"
    assert document.content_type == guess_mime_type(path)
    assert document.metadata["source_system"] == "local"
    assert document.metadata["metadata_schema_version"] == 1
    assert document.metadata["record_id"] == "docs/README.md"
    assert document.metadata["title"] == "README.md"
    assert document.metadata["relative_path"] == "docs/README.md"
    assert len(document.metadata["checksum"]) == 64
    assert "mime_type" not in document.metadata


def test_load_rejects_oversized_files_before_read(tmp_path: Path, monkeypatch):
    path = write_file(tmp_path / "big.txt", b"too large")
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=3))

    def fail_if_read(_path: Path) -> bytes:
        pytest.fail("oversized file was read before its size was rejected")

    monkeypatch.setattr(Path, "read_bytes", fail_if_read)

    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                path.resolve().as_uri(),
                "text/plain",
                str(path.resolve()),
                metadata={"path": str(path.resolve())},
            )
        )


def test_load_rejects_directories(tmp_path: Path):
    connector = LocalFileConnector(config(tmp_path))

    with pytest.raises(DocumentProcessingError, match="not a file"):
        connector.load(SourceRecord(tmp_path.as_uri(), "inode/directory", str(tmp_path)))


def test_process_file_callback_can_skip_or_raise(tmp_path: Path):
    write_file(tmp_path / "keep.md")
    write_file(tmp_path / "skip.md")

    def callback(path: str, _size: int, _mime: str) -> tuple[bool, str]:
        return (not path.endswith("skip.md"), "skip requested")

    connector = LocalFileConnector(config(tmp_path, process_file_callback=callback))

    records = list(connector.discover())

    assert [record.metadata["relative_path"] for record in records] == ["keep.md"]

    def bad_callback(_path: str, _size: int, _mime: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    strict = LocalFileConnector(
        config(tmp_path, process_file_callback=bad_callback, fail_on_error=True)
    )

    with pytest.raises(RuntimeError, match="boom"):
        list(strict.discover())


def test_updated_after_filter_uses_file_mtime(tmp_path: Path):
    old_file = write_file(tmp_path / "old.md")
    write_file(tmp_path / "new.md")
    os.utime(old_file, (946684800, 946684800))
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(
        connector.discover(ConnectorQuery(updated_after=datetime(2024, 1, 1, tzinfo=UTC)))
    )

    assert [record.metadata["relative_path"] for record in records] == ["new.md"]


def test_load_raises_fetch_error_on_read_failure(tmp_path: Path, monkeypatch):
    path = write_file(tmp_path / "secret.md")
    resolved_path = os.path.realpath(str(path))
    original_open = Path.open

    def maybe_raise(self, *args, **kwargs):
        if os.path.realpath(str(self)) == resolved_path:
            raise OSError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", maybe_raise)
    connector = LocalFileConnector(config(tmp_path))

    with pytest.raises(FetchError, match="Could not read local file"):
        connector.load(
            SourceRecord(
                path.resolve().as_uri(),
                "text/plain",
                str(path.resolve()),
                metadata={"path": str(path.resolve())},
            )
        )


def test_load_by_paths_loads_each_file(tmp_path: Path):
    path = write_file(tmp_path / "a.md", b"hello")
    connector = LocalFileConnector(config(tmp_path))

    documents = list(connector.load_by_paths([path]))

    assert [d.content for d in documents] == [b"hello"]


def test_process_file_callback_exception_swallowed_without_fail_on_error(
    tmp_path: Path,
):
    write_file(tmp_path / "a.md")

    def bad_callback(_path: str, _size: int, _mime: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    connector = LocalFileConnector(
        config(tmp_path, process_file_callback=bad_callback, fail_on_error=False)
    )

    assert list(connector.discover()) == []
