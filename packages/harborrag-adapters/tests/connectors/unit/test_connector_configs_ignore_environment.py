"""Connector config dataclasses never take credentials from the process environment."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.jira.config import JiraProjectConfig
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

pytestmark = pytest.mark.unit

_WORKER_ENVIRONMENT = {
    "GITHUB_TOKEN": "worker-github-token",
    "CONFLUENCE_TOKEN": "worker-confluence-token",
    "CONFLUENCE_EMAIL": "worker@example.com",
    "JIRA_TOKEN": "worker-jira-token",
    "JIRA_API_TOKEN": "worker-jira-api-token",
    "JIRA_EMAIL": "worker@example.com",
    "MICROSOFT_TENANT_ID": "worker-tenant",
    "MICROSOFT_CLIENT_ID": "worker-client",
    "MICROSOFT_CLIENT_SECRET": "worker-secret",
    "MICROSOFT_GRAPH_TOKEN": "worker-graph-token",
    "HARBORRAG_INGESTION_TENANT_ID": "worker-harborrag-tenant",
}


@pytest.fixture(autouse=True)
def _worker_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _WORKER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def test_github_config_without_token_stays_anonymous() -> None:
    assert GitHubRepositoryConfig(owner="a", repo="b").token is None


def test_jira_config_without_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="token is required"):
        JiraProjectConfig(base_url="https://jira.example.internal", deployment_type="datacenter")


def test_jira_cloud_config_without_email_is_rejected() -> None:
    with pytest.raises(ValueError, match="email is required"):
        JiraProjectConfig(base_url="https://example.atlassian.net", token="t")


def test_confluence_config_without_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="token is required"):
        ConfluenceSpaceConfig(
            space_key="DEMO",
            base_url="https://wiki.example.internal",
            deployment_type="datacenter",
        )


def test_confluence_cloud_config_without_email_is_rejected() -> None:
    with pytest.raises(ValueError, match="email is required"):
        ConfluenceSpaceConfig(
            space_key="DEMO", base_url="https://example.atlassian.net/wiki", token="t"
        )


def test_sharepoint_config_without_credentials_is_rejected() -> None:
    with pytest.raises(ValueError, match="access_token or client credentials"):
        SharePointSiteConfig(site_url="https://example.sharepoint.com/sites/x")


def test_sharepoint_device_code_config_needs_its_own_ids_and_tenant() -> None:
    with pytest.raises(ValueError, match="requires tenant_id and client_id"):
        SharePointSiteConfig(
            site_url="https://example.sharepoint.com/sites/x", auth_mode="device_code"
        )
    with pytest.raises(ValueError, match="token_cache_path or harborrag_tenant_id"):
        SharePointSiteConfig(
            site_url="https://example.sharepoint.com/sites/x",
            auth_mode="device_code",
            tenant_id="tid",
            client_id="cid",
        )
