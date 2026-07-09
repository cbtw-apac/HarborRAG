from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harborrag_adapters.connectors import LocalFileConfig, LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.local.utils import guess_mime_type
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord


pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def write_file(path: Path, content: bytes | str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def config(source_path: Path, **overrides) -> LocalFileConfig:
    values = {"source_path": source_path}
    values.update(overrides)
    return LocalFileConfig(**values)


def test_config_requires_existing_file_or_folder(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        LocalFileConfig(source_path=tmp_path / "missing")

    cfg = config(tmp_path, allowed_extensions={"md", ".PY"}, excluded_extensions={"tmp"})

    assert cfg.source_path == tmp_path.resolve()
    assert cfg.allowed_extensions == {".md", ".py"}
    assert cfg.excluded_extensions == {".tmp"}


def test_discover_recurses_and_filters_files(tmp_path: Path):
    write_file(tmp_path / "README.md", "# Hello")
    write_file(tmp_path / "src" / "app.py", "print('hi')")
    write_file(tmp_path / "src" / "generated" / "client.py", "generated")
    write_file(tmp_path / "src" / "data.json", "{}")
    write_file(tmp_path / ".secret.md", "hidden")
    write_file(tmp_path / "__pycache__" / "app.pyc", b"compiled")
    connector = LocalFileConnector(
        config(
            tmp_path,
            allowed_extensions={".md", ".py"},
            exclude_globs=["src/generated/*"],
            checksum_mode="stat",
        )
    )

    records = list(connector.discover(ConnectorQuery(pattern="*.*")))

    assert [record.metadata["relative_path"] for record in records] == [
        "README.md",
        "src/app.py",
    ]
    assert records[0].id.startswith("file:")
    assert records[0].source_type == guess_mime_type(tmp_path / "README.md")
    assert records[0].checksum.startswith("stat:")
    assert "mime_type" not in records[0].metadata
    assert "checksum" not in records[0].metadata


def test_discover_supports_non_recursive_and_depth(tmp_path: Path):
    write_file(tmp_path / "root.md")
    write_file(tmp_path / "docs" / "one.md")
    write_file(tmp_path / "docs" / "deep" / "two.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    shallow = list(connector.discover(ConnectorQuery(recursive=False)))
    depth_one = list(connector.discover(ConnectorQuery(filters={}, recursive=True)))

    assert [record.metadata["relative_path"] for record in shallow] == ["root.md"]
    assert [record.metadata["relative_path"] for record in depth_one] == [
        "docs/deep/two.md",
        "docs/one.md",
        "root.md",
    ]

    connector = LocalFileConnector(
        config(tmp_path, allowed_extensions={".md"}, max_depth=1)
    )
    records = list(connector.discover())

    assert [record.metadata["relative_path"] for record in records] == [
        "docs/one.md",
        "root.md",
    ]


def test_discover_supports_direct_file_paths_and_query_filters(tmp_path: Path):
    write_file(tmp_path / "README.md")
    write_file(tmp_path / "notes.txt")
    connector = LocalFileConnector(config(tmp_path))

    records = list(
        connector.discover(
            ConnectorQuery(
                filters={
                    "file_paths": ["README.md", "notes.txt"],
                    "extensions": [".md"],
                }
            )
        )
    )

    assert [record.metadata["relative_path"] for record in records] == ["README.md"]


def test_discover_rejects_paths_outside_source_scope(tmp_path: Path):
    source_root = tmp_path / "scope"
    source_root.mkdir()
    outside = write_file(tmp_path / "outside.md")
    connector = LocalFileConnector(config(source_root))

    with pytest.raises(ValueError, match="outside configured source scope"):
        list(
            connector.discover(
                ConnectorQuery(filters={"file_paths": [str(outside)]})
            )
        )


def test_discover_rejects_direct_symlink_when_symlinks_disabled(tmp_path: Path):
    target = write_file(tmp_path / "target.md")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available in this environment")

    connector = LocalFileConnector(config(tmp_path, follow_symlinks=False))

    with pytest.raises(ValueError, match="symlinks are disabled"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": [link]})))


def test_discover_does_not_follow_symlinks_outside_source_scope(tmp_path: Path):
    source_root = tmp_path / "scope"
    source_root.mkdir()
    outside = tmp_path / "outside"
    write_file(outside / "secret.md")
    link = source_root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available in this environment")

    connector = LocalFileConnector(config(source_root, follow_symlinks=True))

    assert list(connector.discover()) == []


def test_load_reads_file_bytes_and_builds_metadata(tmp_path: Path):
    path = write_file(tmp_path / "docs" / "README.md", b"# Hello")
    connector = LocalFileConnector(config(tmp_path, checksum_mode="sha256"))
    record = next(connector.discover(ConnectorQuery(filters={"file_paths": [path]})))

    document = connector.load(record)

    assert document.id == path.resolve().as_uri()
    assert document.source == path.resolve().as_uri()
    assert document.content == b"# Hello"
    assert document.content_type == guess_mime_type(path)
    assert document.metadata["relative_path"] == "docs/README.md"
    assert len(document.metadata["checksum"]) == 64
    assert "mime_type" not in document.metadata


def test_load_rejects_oversized_files_before_read(tmp_path: Path):
    path = write_file(tmp_path / "big.txt", b"too large")
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=3))

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
        connector.discover(
            ConnectorQuery(updated_after=datetime(2024, 1, 1, tzinfo=UTC))
        )
    )

    assert [record.metadata["relative_path"] for record in records] == ["new.md"]
