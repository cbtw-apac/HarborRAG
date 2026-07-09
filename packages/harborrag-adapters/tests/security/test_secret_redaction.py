"""Credential leakage and error-detail redaction tests."""
from __future__ import annotations

import pytest

from harborrag_adapters.connectors.http_utils import safe_error_detail


pytestmark = pytest.mark.blackbox


def test_connector_config_reprs_redact_secrets() -> None:
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
    from harborrag_adapters.connectors.jira.config import JiraProjectConfig
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

    gh = GitHubRepositoryConfig(owner="o", repo="r", token="ghp_SECRET")
    jira = JiraProjectConfig(
        base_url="https://x.atlassian.net", email="a@b.c", token="jira_SECRET"
    )
    sp = SharePointSiteConfig(
        site_url="https://x.sharepoint.com/sites/s",
        access_token="graph_SECRET",
        client_secret="client_SECRET",
    )
    for text, secret in [
        (repr(gh), "ghp_SECRET"),
        (repr(jira), "jira_SECRET"),
        (repr(sp), "graph_SECRET"),
        (repr(sp), "client_SECRET"),
    ]:
        assert secret not in text


def test_safe_error_detail_truncates_and_redacts() -> None:
    detail = safe_error_detail("token=abc123 " + "A" * 2000)
    assert "abc123" not in detail
    assert len(detail) <= 600
    assert safe_error_detail(None) == ""
