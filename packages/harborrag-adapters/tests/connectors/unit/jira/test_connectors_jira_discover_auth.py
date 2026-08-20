"""Unit tests for Jira connector discovery: credential verification and
authentication-error reclassification."""

from __future__ import annotations

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
)
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


def test_discover_credential_check_failure_always_shows_the_http_status(monkeypatch):
    """Jira Cloud's real 401 body ("Client must be authenticated to access
    this resource.") never mentions a status code, unlike Confluence's 403
    body which happens to embed "403 FORBIDDEN". The message must show the
    number regardless of whether the provider's own wording does, so the two
    providers' failures are equally diagnosable from the raw error text."""
    client = FakeJiraClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError(
            "Client must be authenticated to access this resource.",
            status_code=401,
        )

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError, match=r"\(HTTP 401\)"):
        list(connector.discover())


def test_discover_reclassifies_403_from_credential_check_as_authorization_error(monkeypatch):
    """`myself` needs no resource permissions, so a 403 there means the
    credential checks out but the deployment's edge is rejecting the probe
    itself -- an authorization failure, not an authentication one, and
    distinct from the 401 case above (different reason, same
    "immediate, non-retryable" treatment). Also covers a bare (unwrapped)
    `FetchError(403)`, in case a deployment's edge rejects the probe before
    the shared client gets a chance to raise `AuthorizationError` directly."""
    client = FakeJiraClient()

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            "JIRA request failed with HTTP 403: rejected",
            status_code=403,
            detail="rejected",
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthorizationError, match="JIRA authorization failed: rejected"):
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
        AuthorizationError,
        match="JIRA authorization failed: com.atlassian.jira.some.internal.Exception",
    ):
        list(connector.discover())


def test_discover_reclassifies_a_real_403_as_authorization_error_too():
    """A 403 is an authorization failure regardless of which call it comes
    from -- the shared client (`atlassian/client.py`) raises
    `AuthorizationError` uniformly for any 403 response, not only during the
    `connect()` probe. A 403 from a real search call must therefore also be
    non-retryable, not the generic (retryable) `FetchError` a transient
    5xx/network blip gets: retrying a permission wall for up to 8 attempts
    over several minutes can't ever succeed."""
    client = FakeJiraClient()

    def _raise_forbidden(endpoint, *, json):
        raise AuthorizationError("rejected")

    client.post_json = _raise_forbidden  # type: ignore[method-assign]
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(AuthorizationError, match="rejected"):
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
