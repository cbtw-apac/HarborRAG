"""Unit tests for Jira connector JQL/timestamp/hostname utility helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.utils import (
    build_jql,
    format_query_timestamp,
    is_cloud_hostname,
    search_body,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from jira_test_helpers import FakeJiraClient, cloud_config

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_build_jql_supports_incremental_sync_and_rejects_bad_project_key():
    jql = build_jql(
        project_keys=["ENG"],
        issue_types=["Task"],
        statuses=["In Progress"],
        labels=["rag"],
        updated_after=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
    )

    assert 'project in ("ENG")' in jql
    assert 'issuetype in ("Task")' in jql
    assert 'status in ("In Progress")' in jql
    assert 'labels in ("rag")' in jql
    assert 'updated >= "2024/01/02 03:04"' in jql
    assert "order by updated ASC, key ASC" in jql
    with pytest.raises(ValueError, match="Invalid JIRA project key"):
        build_jql(project_keys=['ENG" OR project = "OPS'])


def test_jql_from_query_normalizes_string_and_list_filters_and_prefers_raw_jql():
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    jql = connector._jql_from_query(
        ConnectorQuery(
            filters={
                "project_keys": "ENG",
                "issue_types": ["Bug", "Task"],
                "jql": "key = ENG-5",
            }
        )
    )

    assert jql == "key = ENG-5"


def test_format_query_timestamp_assumes_utc_for_naive_datetime():
    naive = datetime(2024, 1, 2, 3, 4)
    assert format_query_timestamp(naive) == "2024/01/02 03:04"


def test_is_cloud_hostname_returns_false_for_unparseable_url():
    assert is_cloud_hostname("http://[::1") is False


def test_build_jql_returns_raw_jql_verbatim():
    assert build_jql(raw_jql="key = ENG-9") == "key = ENG-9"


def test_build_jql_supports_filters_without_project_keys():
    jql = build_jql(issue_types=["Bug"])
    assert 'issuetype in ("Bug")' in jql
    assert "project in" not in jql


def test_search_body_omits_expand_when_not_provided():
    body = search_body(jql="x", start_at=0, max_results=10, fields=("summary",))
    assert "expand" not in body
