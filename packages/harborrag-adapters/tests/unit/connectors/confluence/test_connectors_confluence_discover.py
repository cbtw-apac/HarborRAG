"""Unit tests for Confluence connector discovery."""

from __future__ import annotations

import pytest
from confluence_test_helpers import FakeConfluenceClient, cloud_config, light_content
from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
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
    assert client.calls[1][1]["cursor"] == "abc"


def test_discover_supports_direct_content_ids_without_search():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"content_ids": ["1", "2"]}, limit=1)))

    assert [record.locator for record in records] == ["1"]
    assert records[0].id == "confluence://ENG/1"
    assert records[0].metadata["space_key"] == "ENG"


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
    assert [endpoint for endpoint, _params in client.calls] == ["content/1"]


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


def test_content_ids_from_query_accepts_bare_string():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Direct"))
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"content_ids": "1"})))

    assert [r.locator for r in records] == ["1"]
