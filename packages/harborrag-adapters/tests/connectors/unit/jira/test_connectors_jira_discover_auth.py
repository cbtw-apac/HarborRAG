"""Unit tests for Jira connector discovery: credential verification and
authentication-error reclassification."""

from __future__ import annotations

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.exceptions import AuthenticationError, FetchError
from harborrag_adapters.connectors.jira import JiraConnector

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_discover_verifies_credentials_before_any_search_call(monkeypatch):
    """A bad credential must surface as `AuthenticationError` immediately,
    not as an empty search result -- Jira's search endpoint can return HTTP
    200 with no issues for a credential that lacks permission, which used to
    look identical to "no matching issues" from the caller's perspective.

    The message must also carry `connect()`'s own "JIRA authentication
    failed" framing, not just the bare provider text -- otherwise a 401
    (raised directly by the shared client, bypassing `connect()`'s except
    clauses) looks structurally different from a reclassified 403, even
    though both are the same kind of failure from the caller's view."""
    client = FakeJiraClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError, match="JIRA authentication failed: bad credentials"):
        list(connector.discover())

    assert client.post_calls == []


def test_discover_reclassifies_403_from_credential_check_as_authentication_error(monkeypatch):
    """Defensive symmetry with ConfluenceConnector: `myself` needs no resource
    permissions, so if a JIRA deployment's edge ever rejects a bad credential
    with 403 instead of 401, it must surface as `AuthenticationError` too,
    not the generic (retryable) `FetchError` a 403 gets everywhere else --
    with the same "JIRA authentication failed" message shape as the 401
    case, so the two look like the same failure from the caller's view."""
    client = FakeJiraClient()

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            "JIRA request failed with HTTP 403: rejected",
            status_code=403,
            detail="rejected",
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError, match="JIRA authentication failed: rejected"):
        list(connector.discover())


def test_discover_strips_json_envelope_from_credential_check_failure(monkeypatch):
    """A JSON error body from the credential check (as Confluence's often
    is, and Jira's could be too) should surface as its ``message`` field,
    not the raw ``{"statusCode": ..., "message": ...}`` envelope."""
    client = FakeJiraClient()
    detail = (
        '{"statusCode": 403, "message": '
        '"com.atlassian.jira.some.internal.Exception: caller cannot access JIRA"}'
    )

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            f"JIRA request failed with HTTP 403: {detail}",
            status_code=403,
            detail=detail,
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(
        AuthenticationError,
        match="JIRA authentication failed: com.atlassian.jira.some.internal.Exception",
    ):
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
