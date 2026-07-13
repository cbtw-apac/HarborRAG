from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harborrag_adapters.connectors import LocalFileConfig, LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem import LocalFileSystem
from harborrag_adapters.connectors.local.filters import (
    extension_filter,
    file_paths_from_query,
    path_filter,
)
from harborrag_adapters.connectors.local.mappers import path_from_record
from harborrag_adapters.connectors.local.utils import (
    guess_mime_type,
    matches_globs,
    matches_pattern,
    path_in_scope,
    relative_path,
)
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

    cfg = config(
        tmp_path, allowed_extensions={"md", ".PY"}, excluded_extensions={"tmp"}
    )

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

    with pytest.raises(DocumentProcessingError, match="outside configured source scope"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": [str(outside)]})))


def test_discover_rejects_direct_symlink_when_symlinks_disabled(
    tmp_path: Path, monkeypatch
):
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


def test_discover_does_not_follow_symlinks_outside_source_scope(
    tmp_path: Path, monkeypatch
):
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
        connector.load(
            SourceRecord(tmp_path.as_uri(), "inode/directory", str(tmp_path))
        )


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


# --------------------------------------------------------------------------
# config.py validation


def test_config_rejects_special_file_types(tmp_path: Path, monkeypatch):
    fifo_path = tmp_path / "pipe"
    try:
        os.mkfifo(fifo_path)
    except (AttributeError, OSError):
        original_exists = Path.exists
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "exists",
            lambda path: path == fifo_path or original_exists(path),
        )
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda path: False if path == fifo_path else original_is_file(path),
        )
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: False if path == fifo_path else original_is_dir(path),
        )

    with pytest.raises(ValueError, match="must be a regular file or directory"):
        LocalFileConfig(source_path=fifo_path)


def test_config_rejects_invalid_checksum_mode(tmp_path: Path):
    with pytest.raises(ValueError, match="checksum_mode must be one of"):
        config(tmp_path, checksum_mode="crc32")


# --------------------------------------------------------------------------
# filters.py, mappers.py, utils.py pure helpers


def test_extension_filter_accepts_string_value(tmp_path: Path):
    cfg = config(tmp_path)
    query = ConnectorQuery(filters={"allowed_extensions": ".PY"})
    assert extension_filter(cfg, query, "allowed_extensions") == {".py"}

    query_alias = ConnectorQuery(filters={"extensions": "MD"})
    assert extension_filter(cfg, query_alias, "allowed_extensions") == {".md"}


def test_path_filter_accepts_string_value_and_list(tmp_path: Path):
    cfg = config(tmp_path, exclude_paths=["default"])
    query_string = ConnectorQuery(filters={"include_paths": "src"})
    assert path_filter(cfg, query_string, "include_paths") == ["src"]

    query_list = ConnectorQuery(filters={"include_paths": ["src", "docs"]})
    assert path_filter(cfg, query_list, "include_paths") == ["src", "docs"]

    assert path_filter(cfg, ConnectorQuery(), "exclude_paths") == ["default"]


def test_file_paths_from_query_accepts_bare_string_and_path(tmp_path: Path):
    assert file_paths_from_query(ConnectorQuery(filters={"file_paths": "a.md"})) == [
        "a.md"
    ]
    assert file_paths_from_query(ConnectorQuery(filters={"paths": tmp_path})) == [
        tmp_path
    ]
    assert file_paths_from_query(ConnectorQuery()) == []


def test_path_from_record_requires_a_path():
    record = SourceRecord("file:///x", "text/plain", "")
    record.metadata.pop("path", None)
    with pytest.raises(ValueError, match="does not contain path"):
        path_from_record(record)


def test_relative_path_falls_back_when_not_relative(tmp_path: Path):
    other_root = tmp_path / "unrelated"
    other_root.mkdir()
    target = tmp_path / "outside" / "file.txt"
    target.parent.mkdir()
    target.write_text("x")

    assert relative_path(target, other_root) == target.as_posix()


def test_matches_pattern_plain_substring(tmp_path: Path):
    path = tmp_path / "docs" / "guide.md"
    assert matches_pattern(path, tmp_path, "guide") is True
    assert matches_pattern(path, tmp_path, "missing") is False


def test_path_in_scope_matches_and_rejects(tmp_path: Path):
    path = tmp_path / "src" / "app.py"
    assert path_in_scope(path, tmp_path, "src") is True
    assert path_in_scope(path, tmp_path, "docs") is False
    assert path_in_scope(path, tmp_path, "") is True


def test_path_in_scope_is_case_insensitive(tmp_path: Path):
    path = tmp_path / "SRC" / "app.py"
    assert path_in_scope(path, tmp_path, "src") is True
    assert path_in_scope(path, tmp_path, "SRC") is True


def test_matches_globs_is_case_insensitive(tmp_path: Path):
    path = tmp_path / "logs" / "file.log"
    assert matches_globs(path, tmp_path, ["*.LOG"]) is True
    assert matches_globs(path, tmp_path, ["logs/FILE.log"]) is True
    assert matches_globs(path, tmp_path, ["*.txt"]) is False


# --------------------------------------------------------------------------
# filesystem.py traversal edge cases


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
        files = LocalFileSystem(
            config(tmp_path, follow_symlinks=True, allowed_extensions={".md"})
        )
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


def test_iter_files_raises_when_fail_on_error_and_directory_unreadable(
    tmp_path: Path, monkeypatch
):
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


def test_iter_files_skips_symlinked_directory_when_disabled(
    tmp_path: Path, monkeypatch
):
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


def test_should_process_file_rejects_symlink_component_when_disabled(
    tmp_path: Path, monkeypatch
):
    write_file(tmp_path / "real" / "a.md")
    link_dir = tmp_path / "linked"
    try:
        link_dir.symlink_to(tmp_path / "real", target_is_directory=True)
    except (NotImplementedError, OSError):
        write_file(link_dir / "a.md")

    files = LocalFileSystem(config(tmp_path, follow_symlinks=False))
    linked_file = link_dir / "a.md"
    if not link_dir.is_symlink():
        monkeypatch.setattr(
            files,
            "has_symlink_component",
            lambda path: Path(path) == linked_file,
        )

    assert files.should_process_file(linked_file, ConnectorQuery()) is False


def _block_direct_stat_call(blocked: Path, monkeypatch) -> None:
    """Make ``path.stat()`` fail only when called directly by our own code.

    ``should_process_file`` calls ``path.is_file()`` and (via
    ``has_symlink_component``) ``path.is_symlink()`` before its own explicit
    ``stat = path.stat()`` a few lines later — the intended target for its
    ``try/except OSError`` handling. Those helpers stat internally too, so a
    call-count-based trigger fires too early. Instead this only raises when
    ``Path.stat`` is invoked directly from ``should_process_file`` itself (not
    from pathlib's own ``is_file``/``is_symlink``/``lstat`` internals),
    simulating a realistic TOCTOU race (permissions/mount change between the
    early checks and the explicit stat a few lines later) without touching
    source. Comparison uses ``os.path.realpath`` rather than
    ``Path.resolve()``, which itself calls ``Path.stat`` and would recurse.
    """
    import sys

    resolved_blocked = os.path.realpath(str(blocked))
    original_stat = Path.stat

    def maybe_raise(self, *args, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        if caller == "should_process_file" and os.path.realpath(str(self)) == (
            resolved_blocked
        ):
            raise OSError("permission denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", maybe_raise)


def test_should_process_file_skips_unreadable_file(tmp_path: Path, monkeypatch):
    path = write_file(tmp_path / "secret.md")
    _block_direct_stat_call(path, monkeypatch)

    files = LocalFileSystem(config(tmp_path))
    assert files.should_process_file(path, ConnectorQuery()) is False


def test_should_process_file_raises_when_fail_on_error_and_file_unreadable(
    tmp_path: Path, monkeypatch
):
    path = write_file(tmp_path / "secret.md")
    _block_direct_stat_call(path, monkeypatch)

    files = LocalFileSystem(config(tmp_path, fail_on_error=True))
    with pytest.raises(FetchError, match="Could not stat local file"):
        files.should_process_file(path, ConnectorQuery())


def test_should_process_file_include_and_exclude_paths(tmp_path: Path):
    write_file(tmp_path / "src" / "app.py")
    write_file(tmp_path / "docs" / "guide.md")
    connector = LocalFileConnector(config(tmp_path, include_paths=["src"]))
    records = list(connector.discover())
    assert [r.metadata["relative_path"] for r in records] == ["src/app.py"]

    connector = LocalFileConnector(config(tmp_path, exclude_paths=["src"]))
    records = list(connector.discover())
    assert [r.metadata["relative_path"] for r in records] == ["docs/guide.md"]


def test_should_process_file_include_globs_reject_no_match(tmp_path: Path):
    write_file(tmp_path / "a.py")
    connector = LocalFileConnector(config(tmp_path, include_globs=["*.md"]))

    assert list(connector.discover()) == []


def test_enforce_size_limit_noop_without_configured_limit(tmp_path: Path):
    path = write_file(tmp_path / "a.txt", b"hello")
    files = LocalFileSystem(config(tmp_path, max_file_size_bytes=None))
    files.enforce_size_limit(path, 10_000_000)


def test_read_capped_bytes_returns_content_within_limit(tmp_path: Path):
    path = write_file(tmp_path / "a.txt", b"hello")
    files = LocalFileSystem(config(tmp_path, max_file_size_bytes=10))
    assert files.read_capped_bytes(path) == b"hello"


def test_read_capped_bytes_ignores_limit_when_not_configured(tmp_path: Path):
    path = write_file(tmp_path / "a.txt", b"hello")
    files = LocalFileSystem(config(tmp_path, max_file_size_bytes=None))
    assert files.read_capped_bytes(path) == b"hello"


def test_read_capped_bytes_raises_when_content_exceeds_limit_during_read(
    tmp_path: Path,
):
    path = write_file(tmp_path / "big.txt", b"0123456789")
    files = LocalFileSystem(config(tmp_path, max_file_size_bytes=5))

    with pytest.raises(DocumentProcessingError, match="exceeds max_file_size_bytes"):
        files.read_capped_bytes(path)


def test_checksum_mode_none_returns_no_checksum(tmp_path: Path):
    path = write_file(tmp_path / "a.txt")
    files = LocalFileSystem(config(tmp_path, checksum_mode="none"))
    assert files.checksum(path) is None


def test_within_source_scope_for_single_file_source(tmp_path: Path):
    path = write_file(tmp_path / "solo.md")
    files = LocalFileSystem(config(path))
    assert files.within_source_scope(path.resolve()) is True
    assert files.within_source_scope(tmp_path / "other.md") is False


# --------------------------------------------------------------------------
# connector.py load edge cases


def test_discover_stops_at_limit(tmp_path: Path):
    write_file(tmp_path / "a.md")
    write_file(tmp_path / "b.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["relative_path"] for r in records] == ["a.md"]


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


def test_iter_files_skips_symlinked_file_outside_source_scope(
    tmp_path: Path, monkeypatch
):
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


def test_iter_files_silently_skips_dangling_symlink_entries(
    tmp_path: Path, monkeypatch
):
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


def test_should_process_file_rejects_non_file_path(tmp_path: Path):
    files = LocalFileSystem(config(tmp_path))
    assert files.should_process_file(tmp_path, ConnectorQuery()) is False


def test_should_process_file_raises_for_path_outside_source_scope(tmp_path: Path):
    source_root = tmp_path / "scope"
    source_root.mkdir()
    outside = write_file(tmp_path / "outside.md")
    files = LocalFileSystem(config(source_root))

    with pytest.raises(DocumentProcessingError, match="outside configured source scope"):
        files.should_process_file(outside, ConnectorQuery())


def test_should_process_file_rejects_hidden_file_via_direct_path(tmp_path: Path):
    hidden = write_file(tmp_path / ".secret.md")
    connector = LocalFileConnector(config(tmp_path))

    records = list(connector.discover(ConnectorQuery(filters={"file_paths": [hidden]})))

    assert records == []


def test_should_process_file_rejects_pattern_mismatch(tmp_path: Path):
    write_file(tmp_path / "guide.md")
    connector = LocalFileConnector(config(tmp_path, allowed_extensions={".md"}))

    records = list(connector.discover(ConnectorQuery(pattern="missing")))

    assert records == []


def test_should_process_file_excludes_matching_extension(tmp_path: Path):
    write_file(tmp_path / "guide.md")
    write_file(tmp_path / "notes.txt")
    connector = LocalFileConnector(config(tmp_path, excluded_extensions={".md"}))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["notes.txt"]


def test_should_process_file_no_size_limit_configured(tmp_path: Path):
    write_file(tmp_path / "a.md", b"x" * 10_000)
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=None))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["a.md"]


def test_should_process_file_rejects_oversized_file(tmp_path: Path):
    write_file(tmp_path / "big.txt", b"way too large")
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=3))

    records = list(connector.discover())

    assert records == []


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


def test_load_by_paths_loads_each_file(tmp_path: Path):
    path = write_file(tmp_path / "a.md", b"hello")
    connector = LocalFileConnector(config(tmp_path))

    documents = list(connector.load_by_paths([path]))

    assert [d.content for d in documents] == [b"hello"]
