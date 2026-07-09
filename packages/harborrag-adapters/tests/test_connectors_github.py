from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors import GitHubConnector, GitHubRepositoryConfig
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.github.utils import (
    content_endpoint,
    guess_mime_type,
    parse_github_repository_url,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord


class FakeGitHubClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def add(self, endpoint: str, *responses: Any) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        self.calls.append((endpoint, params))
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected GitHub endpoint: {endpoint}")
        return values.pop(0)


def config(**overrides: Any) -> GitHubRepositoryConfig:
    values = {
        "repository_url": "https://github.com/acme/harbor-rag.git",
        "requests_per_minute": 6000,
    }
    values.update(overrides)
    return GitHubRepositoryConfig(**values)


def repo() -> dict[str, Any]:
    return {
        "id": 42,
        "full_name": "acme/harbor-rag",
        "private": False,
        "default_branch": "main",
    }


def commit(ref: str = "commit1", tree_sha: str = "tree-root") -> dict[str, Any]:
    return {
        "sha": ref,
        "html_url": f"https://github.com/acme/harbor-rag/commit/{ref}",
        "commit": {
            "message": "Update docs",
            "author": {
                "name": "Ada",
                "email": "ada@example.com",
                "date": "2024-05-24T20:57:56Z",
            },
            "committer": {
                "name": "Grace",
                "email": "grace@example.com",
                "date": "2024-05-24T21:00:00Z",
            },
            "tree": {"sha": tree_sha},
        },
    }


def tree_item(path: str, sha: str, size: int = 10) -> dict[str, Any]:
    return {
        "path": path,
        "mode": "100644",
        "type": "blob",
        "sha": sha,
        "size": size,
    }


def add_repo_and_commit(client: FakeGitHubClient) -> None:
    client.add("repos/acme/harbor-rag", repo())
    client.add("repos/acme/harbor-rag/commits/main", commit())


def test_config_parses_repository_url_and_normalizes_extensions():
    cfg = config(allowed_extensions={"py", ".MD"}, excluded_extensions={"png"})

    assert cfg.owner == "acme"
    assert cfg.repo == "harbor-rag"
    assert cfg.allowed_extensions == {".py", ".md"}
    assert cfg.excluded_extensions == {".png"}
    assert parse_github_repository_url("git@github.com:acme/harbor-rag.git") == (
        "acme",
        "harbor-rag",
    )


def test_content_endpoint_encodes_paths():
    endpoint = content_endpoint("acme", "harbor-rag", "docs/Hello World.md")

    assert endpoint == "repos/acme/harbor-rag/contents/docs/Hello%20World.md"


def test_discover_uses_recursive_tree_and_filters_files():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "sha": "tree-root",
            "truncated": False,
            "tree": [
                tree_item("README.md", "sha-readme"),
                tree_item("src/app.py", "sha-app"),
                tree_item("src/logo.png", "sha-logo"),
                tree_item("tests/test_app.py", "sha-test"),
            ],
        },
    )
    connector = GitHubConnector(
        config(
            allowed_extensions={".md", ".py"},
            exclude_globs=["tests/*"],
            max_file_size_bytes=100,
        ),
        client=client,
    )

    records = list(connector.discover(ConnectorQuery(path="src", pattern="*.py")))

    assert [record.metadata["path"] for record in records] == ["src/app.py"]
    assert records[0].id == "github://acme/harbor-rag/src/app.py"
    assert records[0].checksum == "sha-app"
    assert records[0].source_type == guess_mime_type("src/app.py")
    assert client.calls[-1] == (
        "repos/acme/harbor-rag/git/trees/tree-root",
        {"recursive": "1"},
    )


def test_discover_falls_back_when_recursive_tree_is_truncated():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {"sha": "tree-root", "truncated": True, "tree": []},
        {
            "sha": "tree-root",
            "tree": [
                {"path": "src", "type": "tree", "sha": "tree-src"},
                tree_item("README.md", "sha-readme"),
            ],
        },
    )
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-src",
        {"sha": "tree-src", "tree": [tree_item("app.py", "sha-app")]},
    )
    connector = GitHubConnector(config(allowed_extensions={".py"}), client=client)

    records = list(connector.discover())

    assert [record.metadata["path"] for record in records] == ["src/app.py"]
    assert client.calls[2] == (
        "repos/acme/harbor-rag/git/trees/tree-root",
        {"recursive": "1"},
    )
    assert client.calls[3] == ("repos/acme/harbor-rag/git/trees/tree-root", None)


def test_discover_supports_direct_file_paths():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    connector = GitHubConnector(config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"file_paths": ["README.md"]}))
    )

    assert records[0].locator == "README.md"
    assert records[0].metadata["sha"] == "sha-readme"


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
    assert document.metadata["repository"] == "acme/harbor-rag"
    assert document.metadata["path"] == "README.md"
    assert document.metadata["commit_author"]["name"] == "Ada"


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
