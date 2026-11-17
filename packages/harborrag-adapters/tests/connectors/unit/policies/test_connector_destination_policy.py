"""Connector clients take their destination policy from the caller, never the environment."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.confluence.client import _RequestsConfluenceClient
from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.github.client import _RequestsGitHubClient
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.jira.client import _RequestsJiraClient
from harborrag_adapters.connectors.jira.config import JiraProjectConfig
from harborrag_adapters.connectors.policies.network import (
    ALLOWED_HOSTS_VARIABLE,
    BlockedDestinationError,
    DestinationPolicy,
)
from harborrag_adapters.connectors.sharepoint.client import _RequestsGraphClient
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

pytestmark = pytest.mark.unit

ClientFactory = Callable[[DestinationPolicy | None], Any]


def _github(policy: DestinationPolicy | None) -> Any:
    config = GitHubRepositoryConfig(owner="acme", repo="handbook")
    return _RequestsGitHubClient(config, destination_policy=policy)


def _jira(policy: DestinationPolicy | None) -> Any:
    config = JiraProjectConfig(
        base_url="https://your-company.atlassian.net",
        email="user@example.com",
        token="token",
        deployment_type="cloud",
    )
    return _RequestsJiraClient(config, destination_policy=policy)


def _confluence(policy: DestinationPolicy | None) -> Any:
    config = ConfluenceSpaceConfig(
        base_url="https://your-company.atlassian.net/wiki",
        email="user@example.com",
        token="token",
        deployment_type="cloud",
        space_key="DEMO",
    )
    return _RequestsConfluenceClient(config, destination_policy=policy)


def _sharepoint(policy: DestinationPolicy | None) -> Any:
    config = SharePointSiteConfig(site_id="site-1", access_token="token")
    return _RequestsGraphClient(config, destination_policy=policy)


CLIENTS = [
    pytest.param(_github, id="github"),
    pytest.param(_jira, id="jira"),
    pytest.param(_confluence, id="confluence"),
    pytest.param(_sharepoint, id="sharepoint"),
]


@pytest.fixture
def local_server() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://localhost:{server.server_port}/"
    server.shutdown()


@pytest.fixture(autouse=True)
def _environment_allows_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VARIABLE, "localhost,127.0.0.0/8,::1")


@pytest.mark.parametrize("make_client", CLIENTS)
def test_a_client_built_without_a_policy_refuses_a_loopback_destination(
    make_client: ClientFactory, local_server: str
) -> None:
    client = make_client(None)

    with pytest.raises(BlockedDestinationError):
        client.session.get(local_server, timeout=5)


@pytest.mark.parametrize("make_client", CLIENTS)
def test_a_client_uses_the_policy_it_is_given(
    make_client: ClientFactory, local_server: str
) -> None:
    client = make_client(DestinationPolicy(hosts=frozenset({"localhost"})))

    assert client.session.get(local_server, timeout=5).status_code == 200


def _outcome_in_another_thread(client: Any, url: str) -> object:
    """Return the status or the exception of a request through that thread's own session."""
    outcome: list[object] = []

    def request() -> None:
        try:
            outcome.append(client._session_for_thread().get(url, timeout=5).status_code)
        except BlockedDestinationError as error:
            outcome.append(error)

    thread = threading.Thread(target=request)
    thread.start()
    thread.join()
    return outcome[0]


@pytest.mark.parametrize("make_client", [_jira, _confluence])
def test_every_atlassian_session_follows_the_given_policy(
    make_client: ClientFactory, local_server: str
) -> None:
    allowed = make_client(DestinationPolicy(hosts=frozenset({"localhost"})))
    strict = make_client(None)

    assert allowed._redirect_session.get(local_server, timeout=5).status_code == 200
    assert _outcome_in_another_thread(allowed, local_server) == 200
    with pytest.raises(BlockedDestinationError):
        strict._redirect_session.get(local_server, timeout=5)
    assert isinstance(_outcome_in_another_thread(strict, local_server), BlockedDestinationError)


@pytest.mark.parametrize("provider", ["confluence", "github", "jira", "sharepoint"])
def test_every_provider_that_opens_a_session_receives_the_policy(provider: str) -> None:
    definition = connector_registry.get_definition(provider)

    assert definition.constructor_dependencies["destination_policy"] == "destination_policy"
