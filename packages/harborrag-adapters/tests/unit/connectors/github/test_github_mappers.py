"""Unit tests for GitHub connector record/metadata mapping helpers."""

from __future__ import annotations

import pytest
from github_test_helpers import repo, tree_item
from harborrag_adapters.connectors.github.mappers import (
    build_document_metadata,
    build_source_record,
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


@pytest.mark.parametrize("item", [{}, {"path": "/"}])
def test_github_mappers_reject_items_without_a_normalized_path(item):
    commit = {
        "sha": "commit1",
        "commit": {"message": "msg", "tree": {"sha": "tree-root"}},
    }

    with pytest.raises(ValueError, match="GitHub item path must not be empty"):
        build_source_record(
            item,
            owner="acme",
            repo="harbor-rag",
            ref="main",
            commit_sha="commit1",
            commit=commit,
        )

    with pytest.raises(ValueError, match="GitHub item path must not be empty"):
        build_document_metadata(
            item,
            owner="acme",
            repo="harbor-rag",
            ref="main",
            commit=commit,
            repository=repo(),
        )
