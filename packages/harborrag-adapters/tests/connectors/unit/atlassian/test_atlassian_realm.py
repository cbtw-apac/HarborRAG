"""Atlassian ACL realms are derived from the configured base URL host."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.atlassian.realm import atlassian_realm

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_atlassian_realm_uses_the_lowercased_host():
    assert atlassian_realm("https://Acme.Atlassian.net/wiki") == "atlassian:acme.atlassian.net"


def test_atlassian_realm_ignores_port_credentials_and_path():
    assert (
        atlassian_realm("https://u:p@Jira.Example.com:8443/jira/") == "atlassian:jira.example.com"
    )


def test_one_host_is_one_realm_for_jira_and_confluence_urls():
    assert atlassian_realm("https://acme.atlassian.net") == atlassian_realm(
        "https://acme.atlassian.net/wiki"
    )


@pytest.mark.parametrize("base_url", ["", "   ", "acme.atlassian.net/wiki", "https:///wiki"])
def test_atlassian_realm_rejects_a_url_without_a_host(base_url: str):
    with pytest.raises(ValueError, match="host"):
        atlassian_realm(base_url)
