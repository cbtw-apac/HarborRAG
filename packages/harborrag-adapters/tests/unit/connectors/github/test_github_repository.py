"""Whitebox unit tests for GitHubRepositoryAPI (tree/blob/commit resolution)."""

from __future__ import annotations

import base64

import pytest
from github_test_helpers import FakeGitHubClient, add_repo_and_commit, config, repo
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.github.repository import GitHubRepositoryAPI

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_iter_tree_raises_when_response_is_not_a_dict():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag/git/trees/tree-root", [1, 2, 3])
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="not an object"):
        list(api.iter_tree("tree-root", recursive=True))


def test_load_blob_raises_when_response_is_not_a_dict():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag/git/blobs/sha-x", [1, 2])
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="not an object"):
        api.load_blob("sha-x")


def test_load_blob_rejects_blob_over_github_api_limit():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-huge",
        {
            "sha": "sha-huge",
            "size": 101 * 1024 * 1024,
            "encoding": "base64",
            "content": "",
        },
    )
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(DocumentProcessingError, match="100 MB REST API limit"):
        api.load_blob("sha-huge")


def test_load_blob_rejects_invalid_base64_content():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-bad",
        {
            "sha": "sha-bad",
            "size": 3,
            "encoding": "base64",
            "content": "not-valid-base64!!!",
        },
    )
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(DocumentProcessingError, match="not valid base64"):
        api.load_blob("sha-bad")


def test_load_blob_rejects_known_oversized_size_without_fetching():
    client = FakeGitHubClient()
    api = GitHubRepositoryAPI(config(max_file_size_bytes=10), client)

    # No blob endpoint response is registered: if the network call happened
    # before the size check, the fake client would raise AssertionError.
    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        api.load_blob("sha-big", known_size=99)

    assert client.calls == []


def test_load_blob_still_fetches_and_decodes_when_known_size_is_within_limit():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag/git/blobs/sha-readme",
        {
            "sha": "sha-readme",
            "size": 7,
            "encoding": "base64",
            "content": base64.b64encode(b"# Hello").decode("ascii"),
        },
    )
    api = GitHubRepositoryAPI(config(max_file_size_bytes=100), client)

    content = api.load_blob("sha-readme", known_size=7)

    assert content == b"# Hello"


def test_content_file_item_raises_when_response_is_not_a_dict():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag/contents/README.md", [1, 2])
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(DocumentProcessingError, match="is not a file"):
        api.content_file_item("README.md", ref="main")


def test_content_file_item_raises_when_response_is_not_a_blob():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag/contents/src",
        {"type": "dir", "path": "src"},
    )
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(DocumentProcessingError, match="is not a file"):
        api.content_file_item("src", ref="main")


def test_record_for_path_resolves_repository_commit_and_item():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    client.add(
        "repos/acme/harbor-rag/contents/README.md",
        {"type": "file", "path": "README.md", "sha": "sha-readme", "size": 7},
    )
    api = GitHubRepositoryAPI(config(), client)

    record = api.record_for_path("README.md")

    assert record.metadata["path"] == "README.md"
    assert record.metadata["sha"] == "sha-readme"


def test_resolve_repository_is_cached():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag", repo())
    api = GitHubRepositoryAPI(config(), client)

    first = api.resolve_repository()
    second = api.resolve_repository()

    assert first is second
    assert len([c for c in client.calls if c[0] == "repos/acme/harbor-rag"]) == 1


def test_resolve_repository_raises_when_full_name_missing():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag", {"id": 1})
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="did not include full_name"):
        api.resolve_repository()


def test_resolve_commit_is_cached():
    client = FakeGitHubClient()
    add_repo_and_commit(client)
    api = GitHubRepositoryAPI(config(), client)
    repository = api.resolve_repository()

    first = api.resolve_commit(repository)
    second = api.resolve_commit(repository)

    assert first is second
    call_count = len(
        [c for c in client.calls if c[0] == "repos/acme/harbor-rag/commits/main"]
    )
    assert call_count == 1


def test_resolve_commit_raises_when_no_ref_is_resolvable():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag", {"full_name": "acme/harbor-rag"})
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="did not include default_branch"):
        api.resolve_commit({"full_name": "acme/harbor-rag"})


def test_resolve_commit_raises_when_response_missing_sha():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag/commits/main", {})
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="did not include sha"):
        api.resolve_commit(repo())


def test_resolve_commit_raises_when_response_missing_tree_sha():
    client = FakeGitHubClient()
    client.add(
        "repos/acme/harbor-rag/commits/main",
        {"sha": "commit1", "commit": {"tree": {}}},
    )
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="did not include tree sha"):
        api.resolve_commit(repo())


def test_walk_tree_non_recursive_raises_when_response_is_not_a_dict():
    client = FakeGitHubClient()
    client.add("repos/acme/harbor-rag/git/trees/tree-root", [1, 2])
    api = GitHubRepositoryAPI(config(), client)

    with pytest.raises(FetchError, match="not an object"):
        list(api._walk_tree_non_recursive("tree-root"))
