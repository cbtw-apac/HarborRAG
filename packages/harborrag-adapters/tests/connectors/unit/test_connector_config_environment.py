"""Connector configs take credentials only from their caller, never from the process.

A source configured without a credential must not borrow the worker's own token:
the worker's environment belongs to the deployment, not to any one source, and the
source fingerprint never covers it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from harborrag_adapters.connectors.confluence import ConfluenceSpaceConfig
from harborrag_adapters.connectors.github import GitHubRepositoryConfig
from harborrag_adapters.connectors.jira import JiraProjectConfig
from harborrag_adapters.connectors.sharepoint import SharePointSiteConfig

pytestmark = [pytest.mark.unit]

_CLOUD = "https://example.atlassian.net"
_DATACENTER = "https://jira.example.com"
_SITE = "https://example.sharepoint.com/sites/knowledge"


def test_github_config_does_not_borrow_the_worker_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "worker-token")

    config = GitHubRepositoryConfig(owner="example", repo="docs")

    assert config.token is None


@pytest.mark.parametrize(
    ("environment", "build", "error"),
    [
        pytest.param(
            {"JIRA_TOKEN": "worker-token", "JIRA_API_TOKEN": "worker-token"},
            lambda: JiraProjectConfig(base_url=_DATACENTER, deployment_type="datacenter"),
            "token is required",
            id="jira-token",
        ),
        pytest.param(
            {"JIRA_EMAIL": "worker@example.com"},
            lambda: JiraProjectConfig(base_url=_CLOUD, token="source-token"),
            "email is required",
            id="jira-email",
        ),
        pytest.param(
            {"CONFLUENCE_TOKEN": "worker-token"},
            lambda: ConfluenceSpaceConfig(
                space_key="ENG", base_url=_DATACENTER, deployment_type="datacenter"
            ),
            "token is required",
            id="confluence-token",
        ),
        pytest.param(
            {"CONFLUENCE_EMAIL": "worker@example.com"},
            lambda: ConfluenceSpaceConfig(space_key="ENG", base_url=_CLOUD, token="source-token"),
            "email is required",
            id="confluence-email",
        ),
        pytest.param(
            {"MICROSOFT_GRAPH_TOKEN": "worker-token", "MICROSOFT_CLIENT_SECRET": "worker-secret"},
            lambda: SharePointSiteConfig(site_url=_SITE),
            "requires access_token or client credentials",
            id="sharepoint-token-and-secret",
        ),
        pytest.param(
            {"MICROSOFT_TENANT_ID": "worker-tenant", "MICROSOFT_CLIENT_ID": "worker-client"},
            lambda: SharePointSiteConfig(site_url=_SITE, auth_mode="device_code"),
            "requires tenant_id and client_id",
            id="sharepoint-app-identity",
        ),
    ],
)
def test_config_requires_credentials_from_its_caller(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    build: Callable[[], object],
    error: str,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=error):
        build()
