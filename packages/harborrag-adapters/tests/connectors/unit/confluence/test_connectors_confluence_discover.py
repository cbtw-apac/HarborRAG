"""Unit tests for Confluence connector discovery."""

from __future__ import annotations

import json

import pytest
from confluence_test_helpers import FakeConfluenceClient, cloud_config, light_content

from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_discover_paginates_and_filters_excluded_labels():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Keep"),
                light_content("2", "Skip", labels=["archived"]),
            ],
            "_links": {"next": "/rest/api/content/search?cursor=abc"},
        },
        {"results": [light_content("3", "Keep Also")], "_links": {}},
    )
    connector = ConfluenceConnector(
        cloud_config(exclude_labels=["archived"]),
        client=client,
    )

    records = list(connector.discover())

    assert [record.metadata["content_id"] for record in records] == ["1", "3"]
    assert records[0].id == "confluence://ENG/1"
    # calls[0] is the auth pre-flight (user/current), calls[1] the first search page.
    assert client.calls[2][1]["cursor"] == "abc"


def test_discover_excludes_live_docs_even_when_content_types_is_page():
    # Live docs report type: "page" -- only subtype: "live" distinguishes
    # them, which the CQL content_types filter can't see, so discovery must
    # reject them client-side regardless of the configured content_types.
    live_doc = light_content("2", "Live Doc")
    live_doc["subtype"] = "live"
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {"results": [light_content("1", "Keep"), live_doc], "_links": {}},
    )
    connector = ConfluenceConnector(
        cloud_config(content_types=["page"], page_size=10),
        client=client,
    )

    records = list(connector.discover())

    assert [record.metadata["content_id"] for record in records] == ["1"]


def test_cql_from_query_treats_pattern_as_safe_text_search_not_raw_cql():
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    cql = connector._cql_from_query(ConnectorQuery(pattern='" OR space = "OTHER'))

    assert 'space = "ENG"' in cql
    assert 'text ~ "\\" OR space = \\"OTHER"' in cql


def test_discover_supports_direct_content_ids_without_search():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"content_ids": ["1", "2"]}, limit=1)))

    assert [record.locator for record in records] == ["1"]
    assert records[0].id == "confluence://ENG/1"
    assert records[0].metadata["space_key"] == "ENG"


def test_discover_carries_exact_attachment_selection_to_admission() -> None:
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    record = next(
        connector.discover(
            ConnectorQuery(
                filters={
                    "content_ids": ["1"],
                    "attachment_ids": ["a2", "a1"],
                }
            )
        )
    )

    assert record.metadata["_selected_attachment_ids"] == (
        "a2",
        "a1",
    )


def test_discover_rejects_direct_content_ids_outside_configured_space():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct", space_key="OPS"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured space"):
        list(connector.discover(ConnectorQuery(filters={"content_ids": ["1"]})))


def test_discover_rejects_query_space_override():
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    with pytest.raises(ValueError, match="outside configured space"):
        list(connector.discover(ConnectorQuery(filters={"space_key": "OPS"})))


def test_discover_can_expand_child_pages_for_direct_ids():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Parent"))
    client.add("content/2", light_content("2", "Child"))
    client.add(
        "content/1/child/page",
        {"results": [light_content("2", "Child")]},
    )
    client.add("content/2/child/page", {"results": []})
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"content_ids": ["1"], "include_children": True}))
    )

    assert [record.locator for record in records] == ["1", "2"]


def test_direct_id_discovery_stops_before_child_traversal_at_limit():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Parent"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(
                filters={"content_ids": ["1"], "include_children": True},
                recursive=True,
                limit=1,
            )
        )
    )

    assert [record.locator for record in records] == ["1"]
    assert [endpoint for endpoint, _params in client.calls if endpoint != "user/current"] == [
        "content/1"
    ]


def test_discover_stops_at_limit_during_search():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {},
        },
    )
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["content_id"] for r in records] == ["1"]


def test_discover_content_types_filter_accepts_list_value():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {"results": [light_content("1", "A")], "_links": {}},
    )
    connector = ConfluenceConnector(cloud_config(page_size=10), client=client)

    records = list(
        connector.discover(ConnectorQuery(filters={"content_types": ["page", "blogpost"]}))
    )

    assert [r.metadata["content_id"] for r in records] == ["1"]


def test_discover_labels_filter_uses_label_alias_and_matches_include_labels():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Match", labels=["runbook"]),
                light_content("2", "NoMatch", labels=["draft"]),
            ],
            "_links": {},
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_labels=["runbook"], page_size=10), client=client
    )

    records = list(connector.discover(ConnectorQuery(filters={"label": "runbook"})))

    assert [r.metadata["content_id"] for r in records] == ["1"]


@pytest.mark.parametrize("content_id", ["1/child/page", "1?expand=body", "#1"])
def test_discover_rejects_unsafe_content_ids(content_id):
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    with pytest.raises(ValueError, match="content ID"):
        list(connector.discover(ConnectorQuery(filters={"content_ids": [content_id]})))


def test_discover_verifies_credentials_before_any_search_call(monkeypatch):
    """A bad credential must surface as `AuthenticationError` immediately,
    mirroring JiraConnector's pre-flight check, instead of only failing once
    a search call happens to be made.

    The message must also carry `connect()`'s own "Confluence
    authentication failed" framing, not just the bare provider text --
    otherwise a 401 (raised directly by the shared client, bypassing
    `connect()`'s except clauses) looks structurally different from a
    reclassified 403, even though both are the same kind of failure from
    the caller's view."""
    client = FakeConfluenceClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(
        AuthenticationError, match="Confluence authentication failed: bad credentials"
    ):
        list(connector.discover())


def test_discover_page_verifies_credentials_before_any_search_call(monkeypatch):
    client = FakeConfluenceClient()

    def _raise_auth_error(endpoint, *, params=None):
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(client, "get_json", _raise_auth_error)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(AuthenticationError):
        connector.discover_page(None, cursor=None, page_size=10)


def test_discover_reclassifies_403_from_credential_check_as_authorization_error(monkeypatch):
    """Confluence Cloud's edge gateway has been observed rejecting an
    invalid/revoked credential on `user/current` with HTTP 403 ("Request
    rejected because caller cannot access Confluence") instead of 401. A 403
    means authorization, not authentication -- the credential itself may be
    fine but lack the scope/permission the edge requires -- so it must
    surface as `AuthorizationError`, distinct from the 401/`AuthenticationError`
    case, not the generic (retryable) `FetchError` a 403 gets everywhere
    else. Also covers a bare (unwrapped) `FetchError(403)`, in case the edge
    rejects the probe before the shared client raises `AuthorizationError`
    directly."""
    client = FakeConfluenceClient()

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            "Confluence request failed with HTTP 403: rejected",
            status_code=403,
            detail="rejected",
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(AuthorizationError, match="Confluence authorization failed: rejected"):
        list(connector.discover())


def test_discover_strips_json_envelope_from_credential_check_failure(monkeypatch):
    """The real Confluence 403 body is JSON with a `message` field wrapping
    an internal Java exception name; the caller should see that message,
    not the raw `{"statusCode": ..., "message": ...}` envelope around it."""
    client = FakeConfluenceClient()
    inner_message = (
        "com.atlassian.confluence.mvc.rest.common.exception."
        'StacklessResponseStatusException: 403 FORBIDDEN "Request rejected '
        'because caller cannot access Confluence"'
    )
    detail = json.dumps({"statusCode": 403, "message": inner_message})

    def _raise_forbidden(endpoint, *, params=None):
        raise FetchError(
            f"Confluence request failed with HTTP 403: {detail}",
            status_code=403,
            detail=detail,
        )

    monkeypatch.setattr(client, "get_json", _raise_forbidden)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(
        AuthorizationError,
        match="Confluence authorization failed: com.atlassian.confluence",
    ):
        list(connector.discover())


def test_discover_page_verifies_credentials_only_once_across_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A")],
            "_links": {"next": "/rest/api/content/search?cursor=tok2"},
        },
        {"results": [light_content("2", "B")], "_links": {}},
    )
    connector = ConfluenceConnector(cloud_config(page_size=1), client=client)

    connector.discover_page(None, cursor=None, page_size=1)
    connector.discover_page(None, cursor="cursor:tok2", page_size=1)

    assert client.calls.count(("user/current", None)) == 1


def test_content_ids_from_query_accepts_bare_string():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"content_ids": "1"})))

    assert [r.locator for r in records] == ["1"]
