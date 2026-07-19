"""Whitebox unit tests for JiraIssueAPI pagination and issue fetching."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors.jira.issues import JiraIssueAPI
from jira_test_helpers import FakeJiraClient, cloud_config, dc_config, issue

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_fetch_comments_stops_when_total_missing_and_page_short():
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "comments": [{"id": "c1", "body": "hi"}]},
    )
    api = JiraIssueAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("ENG-1")

    assert [comment["id"] for comment in comments] == ["c1"]


def test_fetch_changelog_stops_when_total_missing_and_page_short():
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/changelog",
        {
            "startAt": 0,
            "values": [
                {
                    "id": "h1",
                    "author": {"displayName": "Ada"},
                    "created": "2024-05-02T00:00:00.000+0000",
                    "items": [],
                }
            ],
        },
    )
    api = JiraIssueAPI(client, cloud_config(page_size=2))

    histories = api.fetch_changelog("ENG-1")

    assert [history["id"] for history in histories] == ["h1"]


def test_fetch_comments_continues_to_next_page_when_page_is_full():
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "comments": [{"id": "c1"}, {"id": "c2"}]},
        {"startAt": 0, "comments": [{"id": "c3"}]},
    )
    api = JiraIssueAPI(client, cloud_config(page_size=2))

    comments = api.fetch_comments("ENG-1")

    assert [comment["id"] for comment in comments] == ["c1", "c2", "c3"]
    assert client.get_calls[1][1]["startAt"] == 2


def test_fetch_changelog_continues_to_next_page_when_page_is_full():
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/changelog",
        {
            "startAt": 0,
            "values": [{"id": "h1", "items": []}, {"id": "h2", "items": []}],
        },
        {"startAt": 0, "values": [{"id": "h3", "items": []}]},
    )
    api = JiraIssueAPI(client, cloud_config(page_size=2))

    histories = api.fetch_changelog("ENG-1")

    assert [history["id"] for history in histories] == ["h1", "h2", "h3"]
    assert client.get_calls[1][1]["startAt"] == 2


def test_get_issue_does_not_expand_changelog_when_enabled():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    api = JiraIssueAPI(client, cloud_config(include_changelog=True))

    api.get_issue("ENG-1")

    assert "changelog" not in client.get_calls[0][1]["expand"]


def test_search_datacenter_stops_immediately_when_first_page_has_no_issues():
    client = FakeJiraClient()
    client.add_post("search", {"startAt": 0, "issues": []})
    api = JiraIssueAPI(client, dc_config())

    assert list(api.search("order by updated ASC")) == []


def test_search_datacenter_stops_when_total_missing_and_page_short():
    client = FakeJiraClient()
    client.add_post("search", {"startAt": 0, "issues": [issue("ENG-1")]})
    api = JiraIssueAPI(client, dc_config(page_size=2))

    results = list(api.search("order by updated ASC"))

    assert [item["key"] for item in results] == ["ENG-1"]
