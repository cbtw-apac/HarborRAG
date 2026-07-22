"""Whitebox unit tests for LocalFileSystem directory traversal (iter_files)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem import LocalFileSystem
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_files_from_query_source_is_a_single_file(tmp_path: Path):
    file_path = write_file(tmp_path / "solo.md")
    connector = LocalFileConnector(config(file_path))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["solo.md"]


def test_iter_files_raises_for_dangling_or_missing_start_path(tmp_path: Path):
    target = tmp_path / "missing_target.md"
    link = tmp_path / "dangling.md"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pass

    # A dangling symlink as a directory *entry* is silently skipped (neither
    # is_dir() nor is_file() match), so raise only fires when it's the
    # traversal start path itself (via an explicit query.path).
    connector = LocalFileConnector(config(tmp_path, follow_symlinks=True))

    with pytest.raises(DocumentProcessingError, match="not a file or directory"):
        list(connector.discover(ConnectorQuery(path=link)))


def test_iter_files_skips_already_visited_directory_via_symlink_loop(tmp_path: Path):
    nested = tmp_path / "nested"
    nested.mkdir()
    write_file(nested / "a.md")
    loop_link = nested / "loop"
    try:
        loop_link.symlink_to(tmp_path, target_is_directory=True)
    except (NotImplementedError, OSError):
        files = LocalFileSystem(config(tmp_path, follow_symlinks=True, allowed_extensions={".md"}))
        records = list(
            files.iter_files(
                nested,
                query=ConnectorQuery(),
                seen_dirs={nested.resolve()},
            )
        )
        assert records == []
        return

    connector = LocalFileConnector(
        config(tmp_path, follow_symlinks=True, allowed_extensions={".md"})
    )

    records = list(connector.discover())

    assert sorted(r.metadata["relative_path"] for r in records) == ["nested/a.md"]


def _block_iterdir(blocked: Path, monkeypatch) -> None:
    """Force ``Path.iterdir`` to raise OSError only for the given directory.

    Real ``chmod(0o000)`` is unreliable across filesystems/sandboxes (e.g. it is
    frequently a no-op for the owning user in this environment), so the OSError
    branch is exercised deterministically via monkeypatching instead. Comparison
    uses ``os.path.realpath`` (not ``Path.resolve``, which itself calls
    ``Path.stat``/``iterdir`` internally and would recurse into our patch).
    """
    resolved_blocked = os.path.realpath(str(blocked))
    original_iterdir = Path.iterdir

    def maybe_raise(self):
        if os.path.realpath(str(self)) == resolved_blocked:
            raise OSError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", maybe_raise)


def test_iter_files_skips_unreadable_directory(tmp_path: Path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    write_file(blocked / "secret.md")
    _block_iterdir(blocked, monkeypatch)

    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))
    records = list(connector.discover())
    assert records == []


def test_iter_files_raises_when_fail_on_error_and_directory_unreadable(tmp_path: Path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    _block_iterdir(blocked, monkeypatch)

    connector = LocalFileConnector(config(tmp_path, fail_on_error=True))
    with pytest.raises(FetchError, match="Could not list local directory"):
        list(connector.discover())


def test_iter_files_skips_excluded_dir_names(tmp_path: Path):
    write_file(tmp_path / "node_modules" / "pkg" / "index.md")
    write_file(tmp_path / "src" / "keep.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["src/keep.md"]


def test_iter_files_skips_symlinked_directory_when_disabled(tmp_path: Path, monkeypatch):
    real_dir = tmp_path / "real"
    write_file(real_dir / "a.md")
    link = tmp_path / "linked"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except (NotImplementedError, OSError):
        write_file(link / "ignored.md")
        original = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: True if path == link else original(path),
        )

    connector = LocalFileConnector(
        config(tmp_path, follow_symlinks=False, allowed_extensions={".md"})
    )

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["real/a.md"]


def test_iter_files_returns_immediately_for_disabled_symlink_start_path(
    tmp_path: Path, monkeypatch
):
    target = write_file(tmp_path / "target.md")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        write_file(link)
        original = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: True if path == link else original(path),
        )

    files = LocalFileSystem(config(tmp_path, follow_symlinks=False))
    results = list(files.iter_files(link, query=ConnectorQuery()))

    assert results == []


def test_iter_files_continues_after_recursing_into_a_non_last_subdirectory(
    tmp_path: Path,
):
    write_file(tmp_path / "a_dir" / "nested.md")
    write_file(tmp_path / "b_after.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(connector.discover())

    assert sorted(r.metadata["relative_path"] for r in records) == [
        "a_dir/nested.md",
        "b_after.md",
    ]


def test_iter_files_skips_symlinked_file_outside_source_scope(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "scope"
    source_root.mkdir()
    outside = write_file(tmp_path / "outside.md")
    link = source_root / "linked.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        write_file(link)

    connector = LocalFileConnector(config(source_root, follow_symlinks=True))
    if not link.is_symlink():
        from harborrag_adapters.connectors.local import filesystem

        original = filesystem.resolve_path

        def resolve_link(path: str | Path) -> Path:
            return outside.resolve() if Path(path) == link else original(path)

        monkeypatch.setattr(filesystem, "resolve_path", resolve_link)

    assert list(connector.discover()) == []


def test_iter_files_silently_skips_dangling_symlink_entries(tmp_path: Path, monkeypatch):
    write_file(tmp_path / "keep.md")
    dangling = tmp_path / "dangling.md"
    try:
        dangling.symlink_to(tmp_path / "missing_target.md")
    except (NotImplementedError, OSError):
        write_file(dangling)
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda path: False if path == dangling else original_is_file(path),
        )
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: False if path == dangling else original_is_dir(path),
        )

    connector = LocalFileConnector(
        config(tmp_path, follow_symlinks=True, allowed_extensions={".md"})
    )

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["keep.md"]
