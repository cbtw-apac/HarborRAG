"""Unit tests for GitHub connector discovery (tree walk and explicit paths)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from github_test_helpers import (
    FakeGitHubClient,
    add_repo_and_commit,
    config,
    tree_item,
)
from harborrag_adapters.connectors import GitHubConnector
from harborrag_adapters.connectors.github.utils import guess_mime_type
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


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


def test_discover_stops_at_limit_during_explicit_path_iteration():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/a.py",
        {"type": "file", "path": "a.py", "sha": "sha-a", "size": 1},
    )
    client.add(
        "repos/acme/harbor-rag/contents/b.py",
        {"type": "file", "path": "b.py", "sha": "sha-b", "size": 1},
    )
    connector = GitHubConnector(config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(limit=1, filters={"file_paths": ["a.py", "b.py"]})
        )
    )

    assert [r.metadata["path"] for r in records] == ["a.py"]


def test_discover_skips_when_commit_older_than_updated_after():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(updated_after=datetime(2099, 1, 1, tzinfo=UTC))
        )
    )

    assert records == []


def test_discover_non_recursive_query_rejects_nested_paths():
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
            ],
        },
    )
    connector = GitHubConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(recursive=False)))

    assert [r.metadata["path"] for r in records] == ["README.md"]


def test_discover_stops_at_limit_during_tree_walk():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "sha": "tree-root",
            "truncated": False,
            "tree": [
                tree_item("a.py", "sha-a"),
                tree_item("b.py", "sha-b"),
            ],
        },
    )
    connector = GitHubConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["path"] for r in records] == ["a.py"]


def test_discover_explicit_path_filtered_out_continues_to_next_path():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/logo.png",
        {"type": "file", "path": "logo.png", "sha": "sha-logo", "size": 5},
    )
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    connector = GitHubConnector(config(excluded_extensions={".png"}), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(filters={"file_paths": ["logo.png", "README.md"]})
        )
    )

    assert [r.metadata["path"] for r in records] == ["README.md"]


def test_discover_continues_when_commit_is_newer_than_updated_after():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {"sha": "tree-root", "truncated": False, "tree": [tree_item("a.py", "sha-a")]},
    )
    connector = GitHubConnector(config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(updated_after=datetime(2000, 1, 1, tzinfo=UTC))
        )
    )

    assert [r.metadata["path"] for r in records] == ["a.py"]


def test_discover_tree_walk_skips_non_blob_entries():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "sha": "tree-root",
            "truncated": False,
            "tree": [
                {"path": "src", "type": "tree", "sha": "tree-src"},
                tree_item("a.py", "sha-a"),
            ],
        },
    )
    connector = GitHubConnector(config(), client=client)

    records = list(connector.discover())

    assert [r.metadata["path"] for r in records] == ["a.py"]
