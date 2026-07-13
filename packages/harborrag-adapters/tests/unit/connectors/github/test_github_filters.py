"""Unit tests for GitHub connector query/config-based file filtering."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from github_test_helpers import commit, config, tree_item
from harborrag_adapters.connectors.github import filters as github_filters
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _query(**overrides: Any) -> ConnectorQuery:
    return ConnectorQuery(**overrides)


def test_should_process_file_skips_when_commit_older_than_updated_after():
    cfg = config()
    item = tree_item("src/app.py", "sha-app")
    old_commit = commit()
    query = _query(updated_after=datetime(2099, 1, 1, tzinfo=UTC))

    assert not github_filters.should_process_file(cfg, item, query, commit=old_commit)


def test_should_process_file_excludes_by_extension():
    cfg = config(excluded_extensions={".png"})
    item = tree_item("assets/logo.png", "sha-logo")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_include_paths_reject_outside_scope():
    cfg = config(include_paths=["src"])
    item = tree_item("docs/readme.md", "sha-doc")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_exclude_paths_reject_match():
    cfg = config(exclude_paths=["tests"])
    item = tree_item("tests/test_app.py", "sha-test")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_include_globs_reject_no_match():
    cfg = config(include_globs=["*.py"])
    item = tree_item("README.md", "sha-readme")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_exclude_globs_reject_match():
    cfg = config(exclude_globs=["*.md"])
    item = tree_item("README.md", "sha-readme")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_rejects_oversized_file():
    cfg = config(max_file_size_bytes=5)
    item = tree_item("big.txt", "sha-big", size=50)

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_should_process_file_callback_rejection_and_exception_swallowed():
    calls: list[str] = []

    def reject(path: str, size: int, mime: str) -> tuple[bool, str]:
        calls.append(path)
        return False, "not needed"

    cfg = config(process_file_callback=reject)
    item = tree_item("src/app.py", "sha-app")

    assert not github_filters.should_process_file(cfg, item, _query(), commit=commit())
    assert calls == ["src/app.py"]

    def explode(path: str, size: int, mime: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    cfg_swallow = config(process_file_callback=explode, fail_on_error=False)
    assert not github_filters.should_process_file(
        cfg_swallow, item, _query(), commit=commit()
    )

    cfg_raise = config(process_file_callback=explode, fail_on_error=True)
    with pytest.raises(RuntimeError):
        github_filters.should_process_file(cfg_raise, item, _query(), commit=commit())


def test_should_process_file_callback_allows_file():
    def allow(path: str, size: int, mime: str) -> tuple[bool, str]:
        return True, ""

    cfg = config(process_file_callback=allow)
    item = tree_item("src/app.py", "sha-app")

    assert github_filters.should_process_file(cfg, item, _query(), commit=commit())


def test_file_paths_from_query_accepts_string_alias():
    query = _query(filters={"paths": "src/App.PY"})

    assert github_filters.file_paths_from_query(query) == ["src/App.PY"]


def test_file_paths_from_query_returns_empty_when_absent():
    assert github_filters.file_paths_from_query(_query()) == []


def test_extension_filter_accepts_string_and_alias():
    cfg = config()
    query_alias = _query(filters={"extensions": "PY"})
    assert github_filters._extension_filter(cfg, query_alias, "allowed_extensions") == {
        ".py"
    }

    query_string = _query(filters={"excluded_extensions": ".PNG"})
    assert github_filters._extension_filter(
        cfg, query_string, "excluded_extensions"
    ) == {".png"}


def test_path_filter_accepts_string_value():
    cfg = config()
    query = _query(filters={"include_paths": "src"})

    assert github_filters._path_filter(cfg, query, "include_paths") == ["src"]


def test_path_filter_falls_back_to_config_list():
    cfg = config(exclude_paths=["tests"])

    assert github_filters._path_filter(cfg, _query(), "exclude_paths") == ["tests"]


def test_should_process_file_keeps_file_newer_than_updated_after():
    cfg = config()
    item = tree_item("src/app.py", "sha-app")
    query = _query(updated_after=datetime(2000, 1, 1, tzinfo=UTC))

    assert github_filters.should_process_file(cfg, item, query, commit=commit())


def test_extension_filter_accepts_list_of_values():
    cfg = config()
    query = _query(filters={"allowed_extensions": [".PY", "md"]})

    assert github_filters._extension_filter(cfg, query, "allowed_extensions") == {
        ".py",
        ".md",
    }


def test_path_filter_accepts_list_of_values():
    cfg = config()
    query = _query(filters={"include_paths": ["src", "docs"]})

    assert github_filters._path_filter(cfg, query, "include_paths") == [
        "src",
        "docs",
    ]
