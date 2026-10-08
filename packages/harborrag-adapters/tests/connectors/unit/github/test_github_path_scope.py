"""GitHub paths cannot escape the repository or the configured root_path."""

from __future__ import annotations

import pytest
from github_test_helpers import FakeGitHubClient, add_repo_and_commit, config, tree_item

from harborrag_adapters.connectors import GitHubConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.github.repository_paths import (
    normalize_repo_path,
    scoped_root_path,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        "docs/../../x",
        "..",
        "docs\\..\\..\\x",
        "%2e%2e/secrets.txt",
        "docs/%2E%2E/%2e%2e/x",
        "..%2Fsecrets.txt",
        "%2e%2e%5csecrets.txt",
        "%252e%252e/secrets.txt",
    ],
)
def test_normalize_repo_path_rejects_parent_segments(path):
    with pytest.raises(ValueError, match=r"'\.\.'"):
        normalize_repo_path(path)


def test_normalize_repo_path_keeps_ordinary_paths():
    assert normalize_repo_path("/docs/./guide.md") == "docs/guide.md"
    assert normalize_repo_path(None) == ""
    assert normalize_repo_path("docs/v1..2/guide..md") == "docs/v1..2/guide..md"


def test_scoped_root_path_cannot_widen_the_configured_root():
    assert scoped_root_path("docs", None) == "docs"
    assert scoped_root_path("docs", "docs/api") == "docs/api"
    assert scoped_root_path(None, "src") == "src"
    with pytest.raises(ValueError, match="outside configured root_path"):
        scoped_root_path("docs", "src")


def test_explicit_parent_path_never_reaches_the_contents_api():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(ValueError, match=r"'\.\.'"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": ["../../orgs/acme"]})))
    assert all("/contents/" not in endpoint for endpoint, _ in client.calls)


def test_encoded_parent_path_never_reaches_the_contents_api():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(), client=client)

    with pytest.raises(ValueError, match=r"'\.\.'"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": ["%2e%2e/%2e%2e/orgs"]})))
    assert all("/contents/" not in endpoint for endpoint, _ in client.calls)


def test_explicit_path_outside_configured_root_is_rejected():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(root_path="docs"), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        list(connector.discover(ConnectorQuery(filters={"file_paths": ["src/app.py"]})))
    assert all("/contents/" not in endpoint for endpoint, _ in client.calls)


def test_query_path_outside_configured_root_is_rejected():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(root_path="docs"), client=client)

    with pytest.raises(ValueError, match="outside configured root_path"):
        list(connector.discover(ConnectorQuery(path="src")))


def test_load_rejects_record_outside_configured_root():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(root_path="docs"), client=client)
    record = SourceRecord(
        id="github://acme/harbor-rag/src/app.py",
        source_type="text/x-python",
        locator="src/app.py",
        metadata={"path": "src/app.py", "sha": "sha-app"},
    )

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        connector.load(record)
    assert all("/git/blobs/" not in endpoint for endpoint, _ in client.calls)


def test_load_by_paths_rejects_out_of_root_path_before_any_request():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    connector = GitHubConnector(config(root_path="docs"), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        list(connector.load_by_paths(["src/app.py"]))
    assert all("/contents/" not in endpoint for endpoint, _ in client.calls)


def test_remote_tree_entry_with_encoded_parent_segment_is_skipped():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/git/trees/tree-root",
        {
            "tree": [
                tree_item("a/%2e%2e/x.md", "sha-odd"),
                tree_item("docs/guide.md", "sha-guide"),
            ],
            "truncated": False,
        },
    )
    connector = GitHubConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(recursive=True)))

    assert [record.locator for record in records] == ["docs/guide.md"]
