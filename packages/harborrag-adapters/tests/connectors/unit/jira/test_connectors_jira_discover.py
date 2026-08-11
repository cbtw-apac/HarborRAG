"""Unit tests for Jira connector discovery."""

from __future__ import annotations

import pytest
from jira_test_helpers import CLOUD_BASE, FakeJiraClient, cloud_config, dc_config, issue

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.issues import DISCOVERY_FIELDS
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_discover_datacenter_searches_jql_with_start_at_pagination():
    client = FakeJiraClient()
    client.add_post(
        "search",
        {"startAt": 0, "total": 3, "issues": [issue("ENG-1"), issue("ENG-2")]},
        {"startAt": 0, "total": 3, "issues": [issue("ENG-3")]},
    )
    connector = JiraConnector(dc_config(page_size=2), client=client)

    records = list(connector.discover())

    assert [record.metadata["issue_key"] for record in records] == [
        "ENG-1",
        "ENG-2",
        "ENG-3",
    ]
    assert client.post_calls[1][1]["startAt"] == 2
    assert client.post_calls[0][1]["fields"] == list(DISCOVERY_FIELDS)
    assert "expand" not in client.post_calls[0][1]
    assert records[0].id == "jira://ENG/ENG-1"


def test_discover_cloud_uses_search_jql_token_pagination():
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {
            "issues": [issue("ENG-1"), issue("ENG-2")],
            "nextPageToken": "tok2",
            "isLast": False,
        },
        {"issues": [issue("ENG-3")], "isLast": True},
    )
    connector = JiraConnector(cloud_config(), client=client)

    records = list(connector.discover())

    assert [record.metadata["issue_key"] for record in records] == [
        "ENG-1",
        "ENG-2",
        "ENG-3",
    ]
    # Cloud must use the token endpoint and forward nextPageToken. Jira Cloud's
    # search/jql endpoint rejects the request outright (400) if an `expand`
    # key is present at all, so it must never be sent in this body.
    assert client.post_calls[0][0] == "search/jql"
    assert client.post_calls[1][1]["nextPageToken"] == "tok2"
    assert "expand" not in client.post_calls[0][1]
    assert client.post_calls[0][1]["fields"] == list(DISCOVERY_FIELDS)
    assert records[0].id == "jira://ENG/ENG-1"


def test_discover_cloud_rejects_repeated_pagination_token():
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {"issues": [], "nextPageToken": "same", "isLast": False},
        {"issues": [], "nextPageToken": "same", "isLast": False},
    )
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(FetchError, match="did not advance"):
        list(connector.discover())


@pytest.mark.parametrize("token", ["x" * 4097, "unsafe\nvalue"])
def test_discover_cloud_rejects_unsafe_pagination_token(token):
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {"issues": [], "nextPageToken": token, "isLast": False},
    )
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(FetchError, match="did not advance"):
        list(connector.discover())


def test_discover_supports_direct_issue_keys_without_search():
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    records = list(connector.discover(ConnectorQuery(filters={"issue_keys": ["ENG-1"]})))

    assert records[0].locator == "ENG-1"
    assert records[0].metadata["url"] == f"{CLOUD_BASE}/browse/ENG-1"


def test_discover_carries_exact_attachment_selection_to_admission():
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    record = next(
        connector.discover(
            ConnectorQuery(
                filters={
                    "issue_keys": ["ENG-1"],
                    "attachment_ids": ["a2", "a1"],
                }
            )
        )
    )

    assert record.metadata["_selected_attachment_ids"] == (
        "a2",
        "a1",
    )


def test_discover_stops_once_query_limit_reached_during_search():
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {
            "issues": [issue("ENG-1"), issue("ENG-2")],
            "nextPageToken": "tok2",
            "isLast": False,
        },
    )
    connector = JiraConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [record.metadata["issue_key"] for record in records] == ["ENG-1"]
    # Only one page should have been fetched since the limit was hit mid-page.
    assert len(client.post_calls) == 1


def test_issue_keys_from_query_accepts_single_string_value():
    query = ConnectorQuery(filters={"issue_keys": "ENG-1"})
    assert JiraConnector._issue_keys_from_query(query) == ["ENG-1"]


@pytest.mark.parametrize("issue_key", ["ENG-1/comment", "ENG-1?expand=all", "eng-1"])
def test_discover_rejects_unsafe_issue_keys(issue_key):
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    with pytest.raises(ValueError, match="issue key"):
        list(connector.discover(ConnectorQuery(filters={"issue_keys": [issue_key]})))


def test_discover_rejects_out_of_scope_project_from_raw_jql():
    """A raw ``filters["jql"]`` escape hatch must not bypass project scoping."""
    out_of_scope = issue("OTHER-1")
    out_of_scope["fields"]["project"] = {"key": "OTHER", "name": "Other Team"}
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [out_of_scope], "isLast": True})
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured projects"):
        list(connector.discover(ConnectorQuery(filters={"jql": "project = OTHER"})))


def test_discover_verifies_credentials_before_any_search_call(monkeypatch):
    """A bad credential must surface as `AuthenticationError` immediately,
    not as an empty search result -- Jira's search endpoint can return HTTP
    200 with no issues for a credential that lacks permission, which used to
    look identical to "no matching issues" from the caller's perspective."""
    client = FakeJiraClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError):
        list(connector.discover())

    assert client.post_calls == []


def test_discover_reclassifies_403_from_credential_check_as_authentication_error(monkeypatch):
    """Defensive symmetry with ConfluenceConnector: `myself` needs no resource
    permissions, so if a JIRA deployment's edge ever rejects a bad credential
    with 403 instead of 401, it must surface as `AuthenticationError` too,
    not the generic (retryable) `FetchError` a 403 gets everywhere else."""
    client = FakeJiraClient()

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            "JIRA request failed with HTTP 403: rejected",
            status_code=403,
            detail="rejected",
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError):
        list(connector.discover())


def test_discover_does_not_reclassify_403_from_a_real_search_call():
    """The 403->AuthenticationError reclassification is scoped to the
    `myself` credential probe in `connect()`. A 403 from an actual search
    call can legitimately mean "valid credential, no access to this
    project/issue" and must stay a (retryable) `FetchError`."""
    client = FakeJiraClient()

    def _raise_forbidden(endpoint, *, json):
        raise FetchError(
            "JIRA request failed with HTTP 403: rejected",
            status_code=403,
            detail="rejected",
        )

    client.post_json = _raise_forbidden  # type: ignore[method-assign]
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(FetchError):
        list(connector.discover())


def test_discover_page_verifies_credentials_before_any_search_call(monkeypatch):
    client = FakeJiraClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError):
        connector.discover_page(None, cursor=None, page_size=10)

    assert client.post_calls == []


def test_discover_page_verifies_credentials_only_once_across_pages():
    client = FakeJiraClient()
    client.add_post(
        "search/jql",
        {"issues": [issue("ENG-1")], "nextPageToken": "tok2", "isLast": False},
        {"issues": [issue("ENG-2")], "isLast": True},
    )
    connector = JiraConnector(cloud_config(page_size=1), client=client)

    connector.discover_page(None, cursor=None, page_size=1)
    connector.discover_page(None, cursor="token:tok2", page_size=1)

    assert client.get_calls.count(("myself", None)) == 1


def test_discover_rejects_out_of_scope_explicit_issue_key():
    """An explicit ``filters["issue_keys"]`` request must not bypass project
    scoping either: _record_for_key() derives project_key from the key
    prefix alone, with no server round trip to validate it against."""
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    with pytest.raises(DocumentProcessingError, match="outside configured projects"):
        list(connector.discover(ConnectorQuery(filters={"issue_keys": ["OTHER-1"]})))
