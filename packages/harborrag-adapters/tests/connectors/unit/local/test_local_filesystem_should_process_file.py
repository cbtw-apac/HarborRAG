"""Whitebox unit tests for LocalFileSystem.should_process_file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from local_test_helpers import config, write_file

from harborrag_adapters.connectors import LocalFileConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.local.filesystem import LocalFileSystem
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_should_process_file_rejects_symlink_component_when_disabled(tmp_path: Path, monkeypatch):
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
        if caller == "_candidate_stat" and os.path.realpath(str(self)) == resolved_blocked:
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


def test_should_process_file_rejects_oversized_file(tmp_path: Path, caplog):
    write_file(tmp_path / "big.txt", b"way too large")
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=3))

    with caplog.at_level(logging.WARNING, logger="harborrag.adapters.connectors.local"):
        records = list(connector.discover())

    assert records == []
    # Rejecting the file is only half the contract: it must be *reported*, both
    # in the run log and in the structured skip report, never silently omitted.
    assert "big.txt" in caplog.text
    assert "max_file_size_bytes 3" in caplog.text
    assert [(Path(s.path).name, s.reason) for s in connector.skipped] == [
        ("big.txt", "size_limit_exceeded")
    ]
    assert "exceeds size limit: 13 bytes > max_file_size_bytes 3" in connector.skipped[0].detail


def test_discover_reports_oversized_file_alongside_in_limit_file(tmp_path: Path):
    """An in-limit file being discovered must not hide its oversized sibling."""
    write_file(tmp_path / "small.txt", b"ok")
    write_file(tmp_path / "big.txt", b"x" * 100)
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=10))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["small.txt"]
    assert [Path(s.path).name for s in connector.skipped] == ["big.txt"]
    assert connector.skipped[0].reason == "size_limit_exceeded"


def test_discover_skip_report_does_not_duplicate_across_traversals(tmp_path: Path):
    """`discover_page` re-walks the tree per page, so skips must not double-count."""
    write_file(tmp_path / "big.txt", b"x" * 100)
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=10))

    list(connector.discover())
    list(connector.discover())

    assert len(connector.skipped) == 1


def test_discover_page_reports_oversized_file_past_the_page_cutoff(tmp_path: Path):
    """Paging must not hide a skip that falls beyond an early page's cutoff.

    `BaseConnector.discover_page` bounds each page with `query.limit` and
    abandons the generator at the cutoff, so `zbig.txt` is not reached until a
    later page re-walks past it. It must still end up reported exactly once.
    """
    write_file(tmp_path / "a.txt", b"ok")
    write_file(tmp_path / "b.txt", b"ok")
    write_file(tmp_path / "zbig.txt", b"x" * 100)
    connector = LocalFileConnector(config(tmp_path, max_file_size_bytes=10))

    cursor: str | None = None
    pages = 0
    while True:
        page = connector.discover_page(ConnectorQuery(), cursor=cursor, page_size=1)
        pages += 1
        cursor = page.next_cursor
        if cursor is None or pages > 5:
            break

    assert [(Path(s.path).name, s.reason) for s in connector.skipped] == [
        ("zbig.txt", "size_limit_exceeded")
    ]


def test_discover_does_not_report_intended_filter_misses(tmp_path: Path):
    """Extension/glob exclusions are intended filtering, not reportable skips."""
    write_file(tmp_path / "keep.txt", b"ok")
    write_file(tmp_path / "drop.md", b"ok")
    connector = LocalFileConnector(config(tmp_path, excluded_extensions={".md"}))

    records = list(connector.discover())

    assert [r.metadata["relative_path"] for r in records] == ["keep.txt"]
    assert connector.skipped == ()


def test_discover_reports_callback_rejection_with_its_reason(tmp_path: Path):
    write_file(tmp_path / "a.txt", b"ok")
    connector = LocalFileConnector(
        config(
            tmp_path,
            process_file_callback=lambda path, size, mime: (False, "quarantined"),
        )
    )

    records = list(connector.discover())

    assert records == []
    assert connector.skipped[0].reason == "callback_rejected"
    assert "quarantined" in connector.skipped[0].detail
