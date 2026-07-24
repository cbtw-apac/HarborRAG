"""Unit tests for GitHub connector configuration and resource lifecycle."""

from __future__ import annotations

import pytest
from github_test_helpers import FakeGitHubClient, config

from harborrag_adapters.connectors import GitHubConnector, GitHubRepositoryConfig
from harborrag_adapters.connectors.github.repository_paths import parse_github_repository_url

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


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


def test_close_delegates_to_client_close():
    client = FakeGitHubClient()
    closed = []
    client.close = lambda: closed.append(True)
    connector = GitHubConnector(config(), client=client)

    connector.close()

    assert closed == [True]


def test_close_is_a_no_op_when_client_lacks_close():
    connector = GitHubConnector(config(), client=FakeGitHubClient())

    connector.close()  # must not raise


def test_connector_context_manager_calls_close():
    client = FakeGitHubClient()
    closed = []
    client.close = lambda: closed.append(True)

    with GitHubConnector(config(), client=client) as connector:
        assert connector is not None

    assert closed == [True]


def test_config_requires_owner_and_repo():
    with pytest.raises(ValueError, match="owner/repo or repository_url"):
        GitHubRepositoryConfig()


def test_config_rejects_ref_and_branch_together():
    with pytest.raises(ValueError, match="ref or branch, not both"):
        GitHubRepositoryConfig(owner="acme", repo="harbor-rag", ref="v1", branch="main")


def test_config_rejects_commit_sha_with_ref_or_branch():
    with pytest.raises(ValueError, match="commit_sha or ref/branch, not both"):
        GitHubRepositoryConfig(owner="acme", repo="harbor-rag", commit_sha="abc123", ref="v1")


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        GitHubRepositoryConfig(owner="acme", repo="harbor-rag", requests_per_minute=0)
