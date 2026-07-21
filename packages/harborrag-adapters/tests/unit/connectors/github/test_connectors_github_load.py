"""Unit tests for GitHub connector document loading (blob fetch and decode)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from github_test_helpers import FakeGitHubClient, add_repo_and_commit, config
from harborrag_adapters.connectors import GitHubConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_load_decodes_blob_and_builds_metadata():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 7,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    connector = GitHubConnector(config(), client=client)

    document = connector.load(
        SourceRecord(
            "github://acme/harbor-rag/README.md",
            "text/markdown",
            "README.md",
            metadata={"path": "README.md", "sha": "sha-readme", "size": 7},
            checksum="sha-readme",
            updated_at=datetime(2024, 5, 24, tzinfo=UTC),
        )
    )

    assert document.content == b"# Hello"
    assert document.content_type == "text/markdown"
    assert document.source == "https://github.com/acme/harbor-rag/blob/main/README.md"
    assert document.metadata["source_system"] == "github"
    assert document.metadata["metadata_schema_version"] == 1
    assert document.metadata["record_id"] == "README.md"
    assert document.metadata["title"] == "README.md"
    assert document.metadata["owner"] == "acme"
    assert document.metadata["repo"] == "harbor-rag"
    assert document.metadata["path"] == "README.md"
    assert document.metadata["commit_author"]["name"] == "Ada"
    assert "repository" not in document.metadata
    assert "html_url" not in document.metadata
    assert "raw_url" not in document.metadata
    assert "commit_url" not in document.metadata
    assert document.metadata["commit_author"]["date"] == "2024-05-24T20:57:56+00:00"
    assert document.metadata["commit_committer"]["date"] == "2024-05-24T21:00:00+00:00"
    json.dumps(document.metadata)  # datetimes must be JSON-serializable


def test_load_rejects_oversized_blob_before_decode():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-big",
        {
            "sha": "sha-big",
            "size": 99,
            "encoding": "base64",
            "content": base64.b64encode(b"too large").decode("ascii"),
        },
    )
    connector = GitHubConnector(config(max_file_size_bytes=10), client=client)

    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/big.txt",
                "text/plain",
                "big.txt",
                metadata={"path": "big.txt", "sha": "sha-big"},
            )
        )


def test_load_rejects_oversized_single_path_lookup_without_fetching_blob():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/big.txt",
        {"type": "file", "path": "big.txt", "sha": "sha-big", "size": 99},
    )
    connector = GitHubConnector(config(max_file_size_bytes=10), client=client)

    # No response is registered for the blob endpoint: if load_blob were ever
    # called, the fake client would raise AssertionError instead of the
    # expected DocumentProcessingError, proving the blob was never fetched.
    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/big.txt",
                "text/plain",
                "big.txt",
                metadata={"path": "big.txt"},
            )
        )


def test_load_rejects_non_base64_blob():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-bad",
        {"sha": "sha-bad", "size": 7, "encoding": "utf-8", "content": "bad"},
    )
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(DocumentProcessingError, match="unsupported encoding"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/README.md",
                "text/markdown",
                "README.md",
                metadata={"path": "README.md", "sha": "sha-bad"},
            )
        )


def test_load_resolves_sha_via_content_api_when_record_lacks_one():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 7,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    connector = GitHubConnector(config(), client=client)

    document = connector.load(
        SourceRecord(
            "github://acme/harbor-rag/README.md",
            "text/markdown",
            "README.md",
            metadata={"path": "README.md"},
        )
    )

    assert document.content == b"# Hello"


def test_load_raises_when_no_sha_can_be_resolved():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/missing.py",
        {"type": "file", "path": "missing.py", "sha": ""},
    )
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(DocumentProcessingError, match="does not include a blob sha"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/missing.py",
                "text/plain",
                "missing.py",
                metadata={"path": "missing.py"},
            )
        )


def test_load_fills_missing_metadata_size_from_content_length():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 0,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    connector = GitHubConnector(config(), client=client)

    document = connector.load(
        SourceRecord(
            "github://acme/harbor-rag/README.md",
            "text/markdown",
            "README.md",
            metadata={"path": "README.md", "sha": "sha-readme", "size": 0},
        )
    )

    assert document.metadata["size"] == len(b"# Hello")


def test_load_by_paths_loads_each_file():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 7,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    connector = GitHubConnector(config(), client=client)

    documents = list(connector.load_by_paths(["README.md"]))

    assert [d.content for d in documents] == [b"# Hello"]
