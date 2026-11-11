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


def _readme_blob() -> dict[str, object]:
    return {
        "sha": "sha-readme",
        "size": 7,
        "encoding": "base64",
        "content": base64.b64encode(b"# Hello").decode("ascii"),
    }


def test_load_record_with_sha_decodes_blob_and_builds_metadata() -> None:
    """The loaded metadata stays JSON-serializable, commit datetimes included."""
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add("repos/acme/harbor-rag/git/blobs/sha-readme", _readme_blob())
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
    json.dumps(document.metadata)


def test_load_blob_over_max_file_size_raises_before_decode() -> None:
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


def test_load_oversized_content_lookup_raises_without_fetching_blob() -> None:
    """No blob response is registered, so a blob fetch would fail with AssertionError."""
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/big.txt",
        {"type": "file", "path": "big.txt", "sha": "sha-big", "size": 99},
    )
    connector = GitHubConnector(config(max_file_size_bytes=10), client=client)

    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/big.txt",
                "text/plain",
                "big.txt",
                metadata={"path": "big.txt"},
            )
        )


def test_load_non_base64_blob_encoding_raises() -> None:
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


def test_load_record_sha_with_path_segments_raises_before_fetching_blob() -> None:
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(ValueError, match="one URL path segment"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/README.md",
                "text/markdown",
                "README.md",
                metadata={"path": "README.md", "sha": "../../other-repo/git/blobs/abc"},
            )
        )

    assert not any("/git/blobs/" in endpoint for endpoint, _ in client.calls)


def test_load_record_without_sha_resolves_sha_through_contents_api() -> None:
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    client.add("repos/acme/harbor-rag/git/blobs/sha-readme", _readme_blob())
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


def test_load_unresolvable_sha_raises() -> None:
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


def test_load_blob_size_mismatch_raises() -> None:
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

    with pytest.raises(DocumentProcessingError, match="does not match declared size"):
        connector.load(
            SourceRecord(
                "github://acme/harbor-rag/README.md",
                "text/markdown",
                "README.md",
                metadata={"path": "README.md", "sha": "sha-readme", "size": 0},
            )
        )


def test_load_by_paths_paths_inside_root_load_each_file() -> None:
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    client.add("repos/acme/harbor-rag/git/blobs/sha-readme", _readme_blob())
    connector = GitHubConnector(config(), client=client)

    documents = list(connector.load_by_paths(["README.md"]))

    assert [document.content for document in documents] == [b"# Hello"]


def test_load_by_paths_path_outside_configured_root_raises_before_any_request() -> None:
    client = FakeGitHubClient()
    connector = GitHubConnector(config(root_path="docs"), client=client)

    with pytest.raises(ValueError, match="inside the configured root path"):
        list(connector.load_by_paths(["docs/guide.md", "src/app.py"]))

    assert client.calls == []


def test_load_by_paths_parent_segment_raises_before_any_request() -> None:
    client = FakeGitHubClient()
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(ValueError, match=r"must not contain '\.\.' segments"):
        list(connector.load_by_paths(["../../other-repo/contents/x"]))

    assert client.calls == []
