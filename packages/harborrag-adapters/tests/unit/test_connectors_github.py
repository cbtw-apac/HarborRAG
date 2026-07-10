from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors import GitHubConnector, GitHubRepositoryConfig
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.github import filters as github_filters
from harborrag_adapters.connectors.github.mappers import (
    build_document_metadata,
    file_path_from_record,
    parse_timestamp,
)
from harborrag_adapters.connectors.github.repository import GitHubRepositoryAPI
from harborrag_adapters.connectors.github.utils import (
    content_endpoint,
    file_extension,
    github_raw_url,
    guess_mime_type,
    is_tree,
    parse_github_repository_url,
    path_in_scope,
    path_matches_query,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord


pytestmark = [pytest.mark.unit, pytest.mark.graybox]


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
    assert document.source == "https://github.com/acme/harbor-rag/blob/main/README.md"
    assert document.metadata["owner"] == "acme"
    assert document.metadata["repo"] == "harbor-rag"
    assert document.metadata["path"] == "README.md"
    assert document.metadata["commit_author"]["name"] == "Ada"
    assert "repository" not in document.metadata
    assert "html_url" not in document.metadata
    assert "raw_url" not in document.metadata
    assert "commit_url" not in document.metadata


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


# --------------------------------------------------------------------------
# config.py validation


def test_config_requires_owner_and_repo():
    with pytest.raises(ValueError, match="owner/repo or repository_url"):
        GitHubRepositoryConfig()


def test_config_rejects_ref_and_branch_together():
    with pytest.raises(ValueError, match="ref or branch, not both"):
        GitHubRepositoryConfig(owner="acme", repo="harbor-rag", ref="v1", branch="main")


def test_config_rejects_commit_sha_with_ref_or_branch():
    with pytest.raises(ValueError, match="commit_sha or ref/branch, not both"):
        GitHubRepositoryConfig(
            owner="acme", repo="harbor-rag", commit_sha="abc123", ref="v1"
        )


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        GitHubRepositoryConfig(owner="acme", repo="harbor-rag", requests_per_minute=0)


# --------------------------------------------------------------------------
# filters.py predicates


def _query(**overrides: Any) -> ConnectorQuery:
    return ConnectorQuery(**overrides)


def test_should_process_file_skips_when_commit_older_than_updated_after():
    cfg = config()
    item = tree_item("src/app.py", "sha-app")
    old_commit = commit()
    query = _query(updated_after=datetime(2099, 1, 1, tzinfo=UTC))

    assert not github_filters.should_process_file(
        cfg, item, query, commit=old_commit
    )


def test_should_process_file_excludes_by_extension():
    cfg = config(excluded_extensions={".png"})
    item = tree_item("assets/logo.png", "sha-logo")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_include_paths_reject_outside_scope():
    cfg = config(include_paths=["src"])
    item = tree_item("docs/readme.md", "sha-doc")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_exclude_paths_reject_match():
    cfg = config(exclude_paths=["tests"])
    item = tree_item("tests/test_app.py", "sha-test")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_include_globs_reject_no_match():
    cfg = config(include_globs=["*.py"])
    item = tree_item("README.md", "sha-readme")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_exclude_globs_reject_match():
    cfg = config(exclude_globs=["*.md"])
    item = tree_item("README.md", "sha-readme")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_rejects_oversized_file():
    cfg = config(max_file_size_bytes=5)
    item = tree_item("big.txt", "sha-big", size=50)

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )


def test_should_process_file_callback_rejection_and_exception_swallowed():
    calls: list[str] = []

    def reject(path: str, size: int, mime: str) -> tuple[bool, str]:
        calls.append(path)
        return False, "not needed"

    cfg = config(process_file_callback=reject)
    item = tree_item("src/app.py", "sha-app")

    assert not github_filters.should_process_file(
        cfg, item, _query(), commit=commit()
    )
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


# --------------------------------------------------------------------------
# mappers.py edge cases


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


# --------------------------------------------------------------------------
# utils.py pure helpers


def test_parse_github_repository_url_rejects_non_github_scheme():
    with pytest.raises(ValueError, match="absolute GitHub repository URL"):
        parse_github_repository_url("not-a-url")


def test_parse_github_repository_url_rejects_missing_repo_segment():
    with pytest.raises(ValueError, match="must include owner and repository"):
        parse_github_repository_url("https://github.com/acme")


def test_parse_ssh_url_rejects_missing_repo_segment():
    with pytest.raises(ValueError, match="must include owner and repository"):
        parse_github_repository_url("git@github.com:acme")


def test_file_extension_returns_empty_without_dot():
    assert file_extension("README") == ""


def test_path_in_scope_exact_root_match():
    from harborrag_adapters.connectors.github.utils import path_in_scope

    assert path_in_scope("src", "src", recursive=True) is True


def test_path_in_scope_non_recursive_rejects_nested_paths():
    from harborrag_adapters.connectors.github.utils import path_in_scope

    assert path_in_scope("src/pkg/app.py", "src", recursive=False) is False
    assert path_in_scope("src/app.py", "src", recursive=False) is True


def test_path_matches_query_plain_substring():
    assert path_matches_query("src/app.py", "app") is True
    assert path_matches_query("src/app.py", "missing") is False


def test_github_raw_url_default_web_url_uses_raw_githubusercontent():
    from harborrag_adapters.connectors.github.utils import github_raw_url

    url = github_raw_url(
        web_url="https://github.com",
        owner="acme",
        repo="harbor-rag",
        ref="main",
        path="src/app.py",
    )
    assert url == "https://raw.githubusercontent.com/acme/harbor-rag/main/src/app.py"


def test_github_raw_url_custom_web_url_uses_raw_path():
    from harborrag_adapters.connectors.github.utils import github_raw_url

    url = github_raw_url(
        web_url="https://ghe.example.com",
        owner="acme",
        repo="harbor-rag",
        ref="main",
        path="src/app.py",
    )
    assert url == "https://ghe.example.com/acme/harbor-rag/raw/main/src/app.py"


# --------------------------------------------------------------------------
# filters.py remaining branches


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


# --------------------------------------------------------------------------
# repository.py traversal, blob loading, and resolution edge cases


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
        {"sha": "sha-huge", "size": 101 * 1024 * 1024, "encoding": "base64", "content": ""},
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


# --------------------------------------------------------------------------
# connector.py discover/load edge cases


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
        connector.discover(ConnectorQuery(updated_after=datetime(2099, 1, 1, tzinfo=UTC)))
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
        connector.discover(ConnectorQuery(updated_after=datetime(2000, 1, 1, tzinfo=UTC)))
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
