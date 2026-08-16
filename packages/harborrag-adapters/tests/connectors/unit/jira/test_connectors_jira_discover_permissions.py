"""Unit tests for Jira connector discovery: BROWSE_PROJECTS permission-gap
detection on the empty-result path."""

from __future__ import annotations

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, dc_config

from harborrag_adapters.connectors.exceptions import AuthenticationError, FetchError
from harborrag_adapters.connectors.jira import JiraConnector

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


@pytest.mark.parametrize("config_factory", [cloud_config, dc_config])
def test_discover_raises_when_empty_search_hides_a_permission_gap(config_factory):
    """Jira's search endpoint can return HTTP 200 with zero issues for a
    credential that authenticates fine (so `myself` passes) but lacks
    BROWSE_PROJECTS on every project in scope -- indistinguishable from "no
    matching issues" unless discovery double-checks project permission on
    the empty-result path. `mypermissions` keeps the same query params and
    response shape on both Cloud (v3) and Data Center (v2)."""
    client = FakeJiraClient()
    search_endpoint = "search/jql" if config_factory is cloud_config else "search"
    client.add_post(
        search_endpoint,
        {"issues": [], "isLast": True} if config_factory is cloud_config else {"issues": []},
    )
    client.add_get(
        "mypermissions",
        {"permissions": {"BROWSE_PROJECTS": {"havePermission": False}}},
    )
    connector = JiraConnector(config_factory(), client=client)

    with pytest.raises(AuthenticationError, match="BROWSE_PROJECTS"):
        list(connector.discover())

    assert ("mypermissions", {"projectKey": "ENG", "permissions": "BROWSE_PROJECTS"}) in (
        client.get_calls
    )


def test_discover_allows_genuinely_empty_result_when_permission_is_granted():
    """A zero-result search with a confirmed BROWSE_PROJECTS grant is a real
    "no matching issues" -- the permission probe must not turn every empty
    project into a false-positive authentication failure."""
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [], "isLast": True})
    client.add_get(
        "mypermissions",
        {"permissions": {"BROWSE_PROJECTS": {"havePermission": True}}},
    )
    connector = JiraConnector(cloud_config(), client=client)

    assert list(connector.discover()) == []


def test_discover_skips_permission_probe_when_no_project_scope_is_configured():
    """With no configured or requested project scope, there is nothing to
    probe `mypermissions` against, so an empty result passes through as-is
    rather than raising on a scope-less credential."""
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [], "isLast": True})
    connector = JiraConnector(cloud_config(project_keys=[]), client=client)

    assert list(connector.discover()) == []
    assert not any(endpoint == "mypermissions" for endpoint, _ in client.get_calls)


def test_discover_lets_empty_result_stand_when_permission_probe_is_inconclusive():
    """If the `mypermissions` probe itself fails (endpoint disabled,
    unrecognized permission key, transient error), that's inconclusive, not
    a confirmed permission gap -- it must not turn every genuinely-empty
    project into a false-positive `AuthenticationError`. A real bad
    credential still surfaces via the shared client's 401 handling on this
    same call."""
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [], "isLast": True})

    def _raise_fetch_error(endpoint, *, params=None):
        raise FetchError(
            "JIRA request failed with HTTP 400: unrecognized permission",
            status_code=400,
            detail="unrecognized permission",
        )

    original_get_json = client.get_json

    def _get_json(endpoint, *, params=None):
        if endpoint == "mypermissions":
            return _raise_fetch_error(endpoint, params=params)
        return original_get_json(endpoint, params=params)

    client.get_json = _get_json  # type: ignore[method-assign]
    connector = JiraConnector(cloud_config(), client=client)

    assert list(connector.discover()) == []


def test_discover_page_raises_when_empty_search_hides_a_permission_gap():
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [], "isLast": True})
    client.add_get(
        "mypermissions",
        {"permissions": {"BROWSE_PROJECTS": {"havePermission": False}}},
    )
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError, match="BROWSE_PROJECTS"):
        connector.discover_page(None, cursor=None, page_size=10)
