"""Connector base URLs must target public hosts unless explicitly opted in."""

from __future__ import annotations

import socket

import pytest

from harborrag_adapters.connectors import (
    ConfluenceSpaceConfig,
    GitHubRepositoryConfig,
    JiraProjectConfig,
    SharePointSiteConfig,
)
from harborrag_adapters.connectors.policies import validation
from harborrag_adapters.connectors.policies.validation import validate_https_url

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/",
        "https://127.0.0.1/",
        "https://169.254.169.254/latest",
        "https://[::1]/",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://localhost/",
        "https://localhost./",
        "https://100.64.0.1/",
    ],
)
def test_literal_private_hosts_are_rejected(url):
    with pytest.raises(ValueError, match="base_url .*allow_private_network"):
        validate_https_url("base_url", url)


def test_dns_name_resolving_to_a_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(validation, "_resolve_host", lambda _h, _p: ("10.20.30.40",))

    with pytest.raises(ValueError, match="allow_private_network"):
        validate_https_url("base_url", "https://jira.corp.example/")


def test_public_dns_name_is_accepted():
    validate_https_url("base_url", "https://example.atlassian.net")


def test_unresolvable_dns_name_is_deferred_to_connect_time(monkeypatch):
    def unresolvable(hostname: str, _port: int) -> tuple[str, ...]:
        raise validation._DeferredResolution(hostname)

    monkeypatch.setattr(validation, "_resolve_host", unresolvable)

    validate_https_url("base_url", "https://jira.invalid/")


def test_system_resolver_defers_on_resolution_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(validation.socket, "getaddrinfo", fail)

    with pytest.raises(validation._DeferredResolution):
        validation._system_resolve_host("jira.invalid", 443)


def test_allow_private_network_skips_only_the_host_policy(monkeypatch):
    def must_not_resolve(*_args):
        raise AssertionError("host resolution must be skipped when private networks are allowed")

    monkeypatch.setattr(validation, "_resolve_host", must_not_resolve)

    validate_https_url("base_url", "https://10.0.0.5/", allow_private_network=True)
    validate_https_url("base_url", "https://jira.corp.example/", allow_private_network=True)
    with pytest.raises(ValueError, match="HTTPS"):
        validate_https_url("base_url", "http://10.0.0.5/", allow_private_network=True)


@pytest.mark.parametrize(
    "build",
    [
        lambda allow: JiraProjectConfig(
            base_url="https://10.0.0.5", token="pat", allow_private_network=allow
        ),
        lambda allow: ConfluenceSpaceConfig(
            space_key="ENG", base_url="https://10.0.0.5", token="pat", allow_private_network=allow
        ),
        lambda allow: GitHubRepositoryConfig(
            owner="o",
            repo="r",
            api_url="https://10.0.0.5/api/v3",
            web_url="https://10.0.0.5",
            allow_private_network=allow,
        ),
        lambda allow: SharePointSiteConfig(
            site_id="s",
            access_token="t",
            graph_api_url="https://10.0.0.5/v1.0",
            allow_private_network=allow,
        ),
    ],
)
def test_connector_configs_reject_private_hosts_unless_opted_in(build):
    with pytest.raises(ValueError, match="allow_private_network"):
        build(False)
    build(True)
