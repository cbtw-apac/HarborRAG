"""Jira Data Center offset paging when the server answers with a lower maxResults."""

from __future__ import annotations

from typing import Any

import pytest
from jira_test_helpers import FakeJiraClient, dc_config

from harborrag_adapters.connectors.jira.issues import JiraIssueAPI

pytestmark = pytest.mark.unit

SERVER_CAP = 2
REQUESTED = 4
TOTAL = 6


def _capped_page(start_at: int, *, with_total: bool) -> dict[str, Any]:
    count = min(SERVER_CAP, TOTAL - start_at)
    page: dict[str, Any] = {
        "issues": [{"key": f"ENG-{start_at + index + 1}"} for index in range(count)],
        "startAt": start_at,
        "maxResults": SERVER_CAP,
    }
    if with_total:
        page["total"] = TOTAL
    return page


@pytest.mark.parametrize("with_total", [True, False])
def test_search_reads_every_page_when_the_server_lowers_max_results(with_total: bool) -> None:
    client = FakeJiraClient()
    client.add_post(
        "search", *(_capped_page(start, with_total=with_total) for start in (0, 2, 4, 6))
    )
    api = JiraIssueAPI(client, dc_config(page_size=REQUESTED))

    keys = [issue["key"] for issue in api.search("project = ENG")]

    assert keys == [f"ENG-{number}" for number in range(1, TOTAL + 1)]


@pytest.mark.parametrize("with_total", [True, False])
def test_search_page_continues_when_the_server_lowers_max_results(with_total: bool) -> None:
    client = FakeJiraClient()
    client.add_post("search", _capped_page(0, with_total=with_total))
    api = JiraIssueAPI(client, dc_config(page_size=REQUESTED))

    issues, cursor = api.search_page("project = ENG", cursor=None, limit=REQUESTED)

    assert len(issues) == SERVER_CAP
    assert cursor == f"offset:{SERVER_CAP}"


def test_search_page_stops_at_total() -> None:
    client = FakeJiraClient()
    client.add_post("search", _capped_page(4, with_total=True))
    api = JiraIssueAPI(client, dc_config(page_size=REQUESTED))

    issues, cursor = api.search_page("project = ENG", cursor="offset:4", limit=REQUESTED)

    assert len(issues) == SERVER_CAP
    assert cursor is None


def test_fetch_comments_reads_every_page_when_the_server_lowers_max_results() -> None:
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "maxResults": 1, "total": 2, "comments": [{"id": "c1"}]},
        {"startAt": 1, "maxResults": 1, "total": 2, "comments": [{"id": "c2"}]},
    )
    api = JiraIssueAPI(client, dc_config(page_size=REQUESTED))

    comments = api.fetch_comments("ENG-1")

    assert [comment["id"] for comment in comments] == ["c1", "c2"]
