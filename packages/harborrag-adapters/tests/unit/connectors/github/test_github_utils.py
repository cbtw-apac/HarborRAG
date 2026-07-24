"""Unit tests for GitHub connector URL/path/endpoint utility helpers."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.github.utils import (
    content_endpoint,
    file_extension,
    github_raw_url,
    parse_github_repository_url,
    path_in_scope,
    path_matches_query,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_content_endpoint_encodes_paths():
    endpoint = content_endpoint("acme", "harbor-rag", "docs/Hello World.md")

    assert endpoint == "repos/acme/harbor-rag/contents/docs/Hello%20World.md"


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

    assert path_in_scope("src", "src", recursive=True) is True


def test_path_in_scope_non_recursive_rejects_nested_paths():

    assert path_in_scope("src/pkg/app.py", "src", recursive=False) is False
    assert path_in_scope("src/app.py", "src", recursive=False) is True


def test_path_matches_query_plain_substring():
    assert path_matches_query("src/app.py", "app") is True
    assert path_matches_query("src/app.py", "missing") is False


def test_github_raw_url_default_web_url_uses_raw_githubusercontent():

    url = github_raw_url(
        web_url="https://github.com",
        owner="acme",
        repo="harbor-rag",
        ref="main",
        path="src/app.py",
    )
    assert url == "https://raw.githubusercontent.com/acme/harbor-rag/main/src/app.py"


def test_github_raw_url_custom_web_url_uses_raw_path():

    url = github_raw_url(
        web_url="https://ghe.example.com",
        owner="acme",
        repo="harbor-rag",
        ref="main",
        path="src/app.py",
    )
    assert url == "https://ghe.example.com/acme/harbor-rag/raw/main/src/app.py"
