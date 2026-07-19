"""Whitebox unit tests for ConfluenceContentAPI.search pagination."""

from __future__ import annotations

import pytest
from confluence_test_helpers import (
    FakeConfluenceClient,
    cloud_config,
    light_content,
)
from harborrag_adapters.connectors.confluence.content import ConfluenceContentAPI

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_search_returns_immediately_on_empty_first_page():
    client = FakeConfluenceClient()
    client.add("content/search", {"results": []})
    api = ConfluenceContentAPI(client, cloud_config())

    assert list(api.search("type=page")) == []


def test_search_falls_back_to_offset_when_first_full_page_has_no_next_link():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {},
        },
        {"results": [light_content("3", "C")], "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = [item["id"] for item in api.search("type=page")]

    assert ids == ["1", "2", "3"]
    assert client.calls[1][1]["start"] == 2


def test_cursor_search_stops_after_exact_full_final_page_without_next_link():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [light_content("1", "A"), light_content("2", "B")],
            "_links": {"next": "/rest/api/content/search?cursor=next-page"},
        },
        {
            "results": [light_content("3", "C"), light_content("4", "D")],
            "_links": {},
        },
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = [item["id"] for item in api.search("type=page")]

    assert ids == ["1", "2", "3", "4"]
    assert len(client.calls) == 2
    assert client.calls[1][1]["cursor"] == "next-page"
