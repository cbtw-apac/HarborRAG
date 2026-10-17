"""Unit tests for local-filesystem connector discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem_paths import guess_mime_type
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


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
    assert [record.id for record in records] == ["README.md", "src/app.py"]
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

    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}, max_depth=1))
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

    with pytest.raises(DocumentProcessingError, match="outside configured source scope"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": [str(outside)]})))


def test_discover_rejects_direct_symlink_when_symlinks_disabled(tmp_path: Path, monkeypatch):
    target = write_file(tmp_path / "target.md")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        write_file(link)

    connector = LocalFileConnector(config(tmp_path, follow_symlinks=False))
    if not link.is_symlink():
        original = connector._files.has_symlink_component
        monkeypatch.setattr(
            connector._files,
            "has_symlink_component",
            lambda path: Path(path) == link or original(path),
        )

    with pytest.raises(DocumentProcessingError, match="symlinks are disabled"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": [link]})))


def test_discover_does_not_follow_symlinks_outside_source_scope(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "scope"
    source_root.mkdir()
    outside = tmp_path / "outside"
    write_file(outside / "secret.md")
    link = source_root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        link.mkdir()

    connector = LocalFileConnector(config(source_root, follow_symlinks=True))
    if not link.is_symlink():
        from harborrag_adapters.connectors.local import filesystem

        original = filesystem.resolve_path

        def resolve_link(path: str | Path) -> Path:
            return outside.resolve() if Path(path) == link else original(path)

        monkeypatch.setattr(filesystem, "resolve_path", resolve_link)

    assert list(connector.discover()) == []


def _change_after_selection(root: Path, name: str, change):
    """A callback that alters one file after selection, before its record is built."""

    def callback(relative: str, size: int, mime_type: str) -> tuple[bool, str]:
        if Path(relative).name == name:
            change(root / relative)
        return True, ""

    return callback


def test_discover_skips_file_that_vanishes_after_selection(tmp_path: Path):
    # Office lock files such as `~$report.docx` come and go mid-scan.
    write_file(tmp_path / "a.md", "a")
    write_file(tmp_path / "~$report.docx", "lock")
    connector = LocalFileConnector(
        config(
            tmp_path,
            process_file_callback=_change_after_selection(tmp_path, "~$report.docx", Path.unlink),
        )
    )

    records = list(connector.discover())

    assert [record.id for record in records] == ["a.md"]
    assert [(Path(s.path).name, s.reason) for s in connector.skipped] == [
        ("~$report.docx", "unreadable")
    ]


def test_discover_skips_file_that_outgrows_size_cap_before_hashing(tmp_path: Path):
    write_file(tmp_path / "a.md", "a")
    write_file(tmp_path / "grows.log", "small")
    connector = LocalFileConnector(
        config(
            tmp_path,
            checksum_mode="sha256",
            max_file_size_bytes=10,
            process_file_callback=_change_after_selection(
                tmp_path, "grows.log", lambda path: path.write_text("x" * 100)
            ),
        )
    )

    records = list(connector.discover())

    assert [record.id for record in records] == ["a.md"]
    assert [(Path(s.path).name, s.reason) for s in connector.skipped] == [
        ("grows.log", "unreadable")
    ]


def test_discover_raises_for_vanished_file_when_fail_on_error(tmp_path: Path):
    write_file(tmp_path / "gone.md", "a")
    connector = LocalFileConnector(
        config(
            tmp_path,
            fail_on_error=True,
            process_file_callback=_change_after_selection(tmp_path, "gone.md", Path.unlink),
        )
    )

    with pytest.raises(FetchError):
        list(connector.discover())


def test_discover_stops_at_limit(tmp_path: Path):
    write_file(tmp_path / "a.md")
    write_file(tmp_path / "b.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["relative_path"] for r in records] == ["a.md"]
