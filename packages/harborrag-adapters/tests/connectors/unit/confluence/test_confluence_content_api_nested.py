"""Whitebox unit tests for ConfluenceContentAPI comments/attachments/child-page pagination."""

from __future__ import annotations

import pytest
from confluence_test_helpers import FakeConfluenceClient, cloud_config, dc_config

from harborrag_adapters.connectors.confluence.content import ConfluenceContentAPI
from harborrag_adapters.connectors.confluence.query import (
    CLOUD_CONTENT_EXPAND,
    CONTENT_EXPAND,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("config", "expected_expand"),
    [
        (cloud_config(), CLOUD_CONTENT_EXPAND),
        (dc_config(), CONTENT_EXPAND),
    ],
)
def test_get_content_requests_adf_only_when_the_deployment_supports_it(
    config,
    expected_expand,
):
    client = FakeConfluenceClient()
    client.add("content/1", {"id": "1"})

    ConfluenceContentAPI(client, config).get_content("1")

    assert client.calls == [("content/1", {"expand": expected_expand})]


def test_fetch_comments_stops_on_short_final_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {"results": [{"id": "c1"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("1")

    assert [c["id"] for c in comments] == ["c1"]


def test_fetch_comments_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {"results": [{"id": "c1"}, {"id": "c2"}]},
        {"results": [{"id": "c3"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("1")

    assert [c["id"] for c in comments] == ["c1", "c2", "c3"]


def test_fetch_comments_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {
            "results": [{"id": "c1"}, {"id": "c2"}],
            "size": 2,
            "limit": 2,
            "_links": {},
        },
        {"results": [{"id": "c3"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    comments = api.fetch_comments("1")

    assert [comment["id"] for comment in comments] == ["c1", "c2", "c3"]
    assert client.calls[1][1]["start"] == 2


def test_list_attachments_stops_on_short_final_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {"results": [{"id": "a1"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    attachments = api.list_attachments("1")

    assert [a["id"] for a in attachments] == ["a1"]


def test_list_attachments_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {"results": [{"id": "a1"}, {"id": "a2"}]},
        {"results": [{"id": "a3"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    attachments = api.list_attachments("1")

    assert [a["id"] for a in attachments] == ["a1", "a2", "a3"]


def test_list_attachments_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {
            "results": [{"id": "a1"}, {"id": "a2"}],
            "size": 2,
            "limit": 2,
            "_links": {"next": "/next"},
        },
        {"results": [{"id": "a3"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    attachments = api.list_attachments("1")

    assert [attachment["id"] for attachment in attachments] == ["a1", "a2", "a3"]
    assert client.calls[1][1]["start"] == 2


def test_fetch_comments_truncates_at_max_comments_without_fetching_next_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/comment",
        {"results": [{"id": "c1"}, {"id": "c2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2, max_comments=1))

    comments = api.fetch_comments("1")

    assert [c["id"] for c in comments] == ["c1"]
    assert len(client.calls) == 1


def test_list_attachments_truncates_at_max_attachments_without_fetching_next_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/attachment",
        {"results": [{"id": "a1"}, {"id": "a2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2, max_attachments=1))

    attachments = api.list_attachments("1")

    assert [a["id"] for a in attachments] == ["a1"]
    assert len(client.calls) == 1


def test_with_children_skips_duplicate_ids():
    api = ConfluenceContentAPI(FakeConfluenceClient(), cloud_config())

    ids = list(api.with_children(["1", "1", "2"], ConnectorQuery()))

    assert ids == ["1", "2"]


def test_child_page_ids_skips_already_seen_and_missing_ids():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}, {}, {"id": "2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=10))

    ids = list(api.child_page_ids("1", recursive=False, seen={"1"}))

    assert ids == ["2"]


def test_child_page_ids_non_recursive_does_not_descend():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config())

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2"]
    assert ("content/2/child/page", None) not in [
        (c[0], c[1]) for c in client.calls if "start" not in (c[1] or {})
    ]


def test_child_page_ids_paginates_across_multiple_pages():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}, {"id": "3"}]},
        {"results": [{"id": "4"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2))

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2", "3", "4"]


def test_child_page_ids_truncates_at_max_child_pages_without_fetching_next_page():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [{"id": "2"}, {"id": "3"}]},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=2, max_child_pages=1))

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2"]
    assert len(client.calls) == 1


def test_child_page_ids_truncates_across_multiple_root_ids_independently():
    # max_child_pages caps discovery per root, not globally across a batch of
    # roots requested through with_children -- each root gets its own budget.
    client = FakeConfluenceClient()
    client.add("content/1/child/page", {"results": [{"id": "2"}, {"id": "3"}]})
    client.add("content/10/child/page", {"results": [{"id": "11"}, {"id": "12"}]})
    api = ConfluenceContentAPI(client, cloud_config(page_size=10, max_child_pages=1))

    ids = list(
        api.with_children(
            ["1", "10"],
            ConnectorQuery(filters={"include_children": True}, recursive=False),
        )
    )

    assert ids == ["1", "2", "10", "11"]


def test_child_page_ids_honors_server_clamped_page_limit():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {
            "results": [{"id": "2"}, {"id": "3"}],
            "size": 2,
            "limit": 2,
            "_links": {"next": "/next"},
        },
        {"results": [{"id": "4"}], "size": 1, "limit": 2, "_links": {}},
    )
    api = ConfluenceContentAPI(client, cloud_config(page_size=100))

    ids = list(api.child_page_ids("1", recursive=False, seen=set()))

    assert ids == ["2", "3", "4"]
    assert client.calls[1][1]["start"] == 2
