"""Unit tests for local-filesystem connector filter/mapper/path utility helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from local_test_helpers import config

from harborrag_adapters.connectors.local.filters import (
    extension_filter,
    file_paths_from_query,
    path_filter,
)
from harborrag_adapters.connectors.local.mappers import path_from_record
from harborrag_adapters.connectors.local.utils import (
    matches_globs,
    matches_pattern,
    path_in_scope,
    relative_path,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
    assert file_paths_from_query(ConnectorQuery(filters={"file_paths": "a.md"})) == ["a.md"]
    assert file_paths_from_query(ConnectorQuery(filters={"paths": tmp_path})) == [tmp_path]
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
