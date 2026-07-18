"""Unit tests for GitHub connector record/metadata mapping helpers."""

from __future__ import annotations

import pytest
from github_test_helpers import repo, tree_item
from harborrag_adapters.connectors.github.mappers import (
    build_document_metadata,
    file_path_from_record,
    parse_timestamp,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_parse_timestamp_handles_missing_and_invalid_values():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-timestamp") is None


def test_file_path_from_record_requires_a_path():
    record = SourceRecord("github://acme/harbor-rag/x", "text/plain", "")
    record.metadata.pop("path", None)

    with pytest.raises(ValueError, match="does not contain path"):
        file_path_from_record(record)


def test_build_document_metadata_handles_missing_commit_identity():
    item = tree_item("README.md", "sha-readme")
    commit_without_identities = {
        "sha": "commit1",
        "commit": {"message": "msg", "tree": {"sha": "tree-root"}},
    }

    metadata = build_document_metadata(
        item,
        owner="acme",
        repo="harbor-rag",
        ref="main",
        commit=commit_without_identities,
        repository=repo(),
    )

    assert metadata.commit_author is None
    assert metadata.commit_committer is None
