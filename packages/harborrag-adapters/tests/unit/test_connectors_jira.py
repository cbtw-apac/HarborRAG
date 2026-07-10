from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.jira import (
    JiraConnector,
    JiraDeploymentType,
    JiraProjectConfig,
)
from harborrag_adapters.connectors.jira.content import (
    _display_name as content_display_name,
)
from harborrag_adapters.connectors.jira.content import (
    _name as content_name,
)
from harborrag_adapters.connectors.jira.content import (
    _walk_adf,
    build_raw_content,
    field_text,
)
from harborrag_adapters.connectors.jira.issues import DISCOVERY_FIELDS, JiraIssueAPI
from harborrag_adapters.connectors.jira.mappers import (
    build_document_metadata,
    issue_key_from_record,
    parse_timestamp,
)
from harborrag_adapters.connectors.jira.utils import (
    build_jql,
    format_query_timestamp,
    is_cloud_hostname,
    search_body,
)
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


CLOUD_BASE = "https://example.atlassian.net"
DC_BASE = "https://jira.example.com"


class FakeJiraClient:
    def __init__(self) -> None:
        self.get_responses: dict[str, list[dict[str, Any]]] = {}
        self.post_responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def add_get(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.get_responses.setdefault(endpoint, []).extend(responses)

    def add_post(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.post_responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_calls.append((endpoint, params))
        values = self.get_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA GET endpoint: {endpoint}")
        return values.pop(0)

    def post_json(self, endpoint: str, *, json: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append((endpoint, json))
        values = self.post_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA POST endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeAttachmentParser:
    def parse(self, input) -> ParsedDocument:
        return ParsedDocument(content=f"parsed:{input.filename}", parser_name="fake")


def cloud_config(**overrides: Any) -> JiraProjectConfig:
    values = {
        "base_url": CLOUD_BASE,
        "token": "token",
        "email": "me@example.com",
        "project_keys": ["ENG"],
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return JiraProjectConfig(**values)


def dc_config(**overrides: Any) -> JiraProjectConfig:
    values = {
        "base_url": DC_BASE,
        "token": "pat",
        "project_keys": ["ENG"],
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return JiraProjectConfig(**values)


def issue(key: str = "ENG-1") -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": "Build parser",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "ADF body"}],
                    }
                ],
            },
            "issuetype": {"name": "Task"},
            "status": {"name": "In Progress", "statusCategory": {"name": "Doing"}},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Ada"},
            "reporter": {"displayName": "Grace"},
            "creator": {"displayName": "Linus"},
            "labels": ["rag"],
            "project": {"key": "ENG", "name": "Engineering"},
            "components": [{"name": "Adapters"}],
            "fixVersions": [{"name": "1.0"}],
            "versions": [{"name": "0.9"}],
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-05-24T20:57:56.130+0000",
            "resolutiondate": None,
            "duedate": "2024-06-01",
            "parent": {"id": "10000", "key": "ENG-0", "fields": {"summary": "Epic"}},
            "subtasks": [],
            "issuelinks": [],
            "customfield_10010": {"value": "Platform"},
            "customfield_10011": [
                {"value": "Docs"},
                {"value": "Search"},
            ],
            "attachment": [
                {
                    "id": "a1",
                    "filename": "notes.md",
                    "mimeType": "text/markdown",
                    "size": 12,
                    "content": f"{CLOUD_BASE}/secure/attachment/a1/notes.md",
                }
            ],
        },
        "names": {
            "customfield_10010": "Impact Area",
            "customfield_10011": "Teams",
        },
        "schema": {
            "customfield_10010": {
                "type": "option",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            },
            "customfield_10011": {
                "type": "array",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiselect",
            },
        },
    }


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == JiraDeploymentType.CLOUD
    assert dc_config().deployment_type == JiraDeploymentType.DATACENTER


def test_config_requires_cloud_email():
    with pytest.raises(ValueError, match="email is required"):
        JiraProjectConfig(base_url=CLOUD_BASE, token="token")


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
    with pytest.raises(ValueError):
        build_jql(project_keys=['ENG" OR project = "OPS'])


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


def test_discover_supports_direct_issue_keys_without_search():
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    records = list(
        connector.discover(ConnectorQuery(filters={"issue_keys": ["ENG-1"]}))
    )

    assert records[0].locator == "ENG-1"
    assert records[0].metadata["url"] == f"{CLOUD_BASE}/browse/ENG-1"


def test_load_builds_raw_document_comments_attachments_and_changelog():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    client.add_get(
        "issue/ENG-1/comment",
        {
            "startAt": 0,
            "total": 1,
            "comments": [
                {
                    "id": "c1",
                    "author": {"displayName": "Bob"},
                    "body": "Looks good",
                    "created": "2024-05-01T00:00:00.000+0000",
                }
            ],
        },
    )
    client.add_get(
        "issue/ENG-1/changelog",
        {
            "startAt": 0,
            "total": 1,
            "values": [
                {
                    "id": "h1",
                    "author": {"displayName": "Ada"},
                    "created": "2024-05-02T00:00:00.000+0000",
                    "items": [
                        {
                            "field": "status",
                            "fromString": "Open",
                            "toString": "Done",
                        }
                    ],
                }
            ],
        },
    )
    client.downloads[f"{CLOUD_BASE}/secure/attachment/a1/notes.md"] = b"# Notes"
    connector = JiraConnector(
        cloud_config(include_attachments=True, include_changelog=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))

    assert document.id == "jira://ENG/ENG-1"
    assert document.content_type == "text/markdown"
    assert "# ENG-1 Build parser" in document.content
    assert "ADF body" in document.content
    assert "Impact Area: Platform" in document.content
    assert "Teams: Docs" in document.content
    assert "Bob: Looks good" in document.content
    assert "parsed:notes.md" in document.content
    assert document.source == f"{CLOUD_BASE}/browse/ENG-1"
    assert document.metadata["assignee"] == "Ada"
    assert document.metadata["reporter"] == "Grace"
    assert document.metadata["custom_fields"][0]["field_id"] == "customfield_10010"
    assert document.metadata["custom_fields"][0]["name"] == "Impact Area"
    assert document.metadata["custom_fields"][0]["text"] == "Platform"
    assert document.metadata["custom_fields"][1]["text"] == "Docs\nSearch"
    processed_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "processed"
    ]
    assert len(processed_attachments) == 1
    assert "attachments_summary" not in document.metadata
    assert "url" not in document.metadata
    assert document.metadata["changelog"][0]["items"][0]["field"] == "status"
    assert client.get_calls[0][1]["fields"] == "*all"
    assert "names" in client.get_calls[0][1]["expand"]
    assert "schema" in client.get_calls[0][1]["expand"]


def test_load_skips_cross_origin_attachment_download_urls():
    bad_issue = issue()
    bad_issue["fields"]["attachment"][0]["content"] = "https://evil.example/notes.md"
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", bad_issue)
    client.add_get("issue/ENG-1/comment", {"startAt": 0, "total": 0, "comments": []})
    connector = JiraConnector(
        cloud_config(include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))

    skipped_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "skipped"
    ]
    assert len(skipped_attachments) == 1
    assert "outside trusted origin" in document.metadata["attachments"][0]["reason"]


def test_load_rejects_comments_over_configured_limit():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    client.add_get(
        "issue/ENG-1/comment",
        {
            "startAt": 0,
            "total": 2,
            "comments": [
                {"id": "c1", "body": "one"},
                {"id": "c2", "body": "two"},
            ],
        },
    )
    connector = JiraConnector(
        cloud_config(max_comments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_comments"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


def test_load_rejects_attachments_over_configured_limit():
    loaded_issue = issue()
    loaded_issue["fields"]["attachment"].append(
        {
            "id": "a2",
            "filename": "two.md",
            "mimeType": "text/markdown",
            "size": 12,
            "content": f"{CLOUD_BASE}/secure/attachment/a2/two.md",
        }
    )
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", loaded_issue)
    connector = JiraConnector(
        cloud_config(
            include_comments=False,
            include_attachments=True,
            max_attachments=1,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_attachments"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


def test_load_raises_on_missing_required_fields():
    bad = issue()
    bad["fields"].pop("summary")
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", bad)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="summary"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


# ---------------------------------------------------------------------------
# config.py edge cases
# ---------------------------------------------------------------------------


def test_config_requires_token_when_env_vars_absent(monkeypatch):
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token is required"):
        JiraProjectConfig(
            base_url=DC_BASE, deployment_type=JiraDeploymentType.DATACENTER
        )


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute"):
        dc_config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size"):
        dc_config(page_size=0)


def test_config_requested_fields_returns_explicit_fields_when_not_all():
    config = dc_config(include_all_fields=False, fields=("summary", "status"))
    assert config.requested_fields() == ("summary", "status")


# ---------------------------------------------------------------------------
# connector.py edge cases
# ---------------------------------------------------------------------------


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


def test_load_by_keys_yields_documents_for_each_key():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue("ENG-1"))
    client.add_get("issue/ENG-1/comment", {"startAt": 0, "total": 0, "comments": []})
    client.add_get("issue/ENG-2", issue("ENG-2"))
    client.add_get("issue/ENG-2/comment", {"startAt": 0, "total": 0, "comments": []})
    connector = JiraConnector(cloud_config(), client=client)

    documents = list(connector.load_by_keys(["ENG-1", "ENG-2"]))

    assert [document.id for document in documents] == [
        "jira://ENG/ENG-1",
        "jira://ENG/ENG-2",
    ]


def test_issue_keys_from_query_accepts_single_string_value():
    query = ConnectorQuery(filters={"issue_keys": "ENG-1"})
    assert JiraConnector._issue_keys_from_query(query) == ["ENG-1"]


@pytest.mark.parametrize("issue_key", ["ENG-1/comment", "ENG-1?expand=all", "eng-1"])
def test_discover_rejects_unsafe_issue_keys(issue_key):
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    with pytest.raises(ValueError, match="issue key"):
        list(connector.discover(ConnectorQuery(filters={"issue_keys": [issue_key]})))


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


# ---------------------------------------------------------------------------
# issues.py edge cases
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# content.py edge cases (pure helpers, tested directly)
# ---------------------------------------------------------------------------


def test_field_text_handles_none_scalar_and_plain_dict_fallback():
    assert field_text(None) == ""
    assert field_text(42) == "42"
    assert field_text({"foo": "bar"}) == "bar"


def test_field_text_extracts_html():
    assert "hi" in field_text("<p>hi</p>")


def test_walk_adf_handles_string_list_hardbreak_and_other_scalars():
    assert _walk_adf("plain") == ["plain"]
    assert _walk_adf(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    ) == [
        "a",
        "b",
    ]
    assert _walk_adf({"type": "hardBreak"}) == ["\n"]
    assert _walk_adf(42) == []


def test_content_name_and_display_name_handle_missing_values():
    assert content_name(None) is None
    assert content_name({"nokey": 1}) is None
    assert content_display_name("not-a-dict") is None
    assert content_display_name({}) is None


def test_build_raw_content_skips_custom_fields_section_when_absent():
    minimal_issue = {"key": "ENG-1", "fields": {"summary": "Title"}}

    content = build_raw_content(minimal_issue)

    assert "## Custom Fields" not in content


# ---------------------------------------------------------------------------
# mappers.py edge cases
# ---------------------------------------------------------------------------


def test_parse_timestamp_returns_none_for_invalid_string():
    assert parse_timestamp("not-a-timestamp") is None


def test_issue_key_from_record_requires_issue_key():
    record = SourceRecord(id="jira://x", source_type="jira", locator="")

    with pytest.raises(ValueError, match="does not contain issue_key"):
        issue_key_from_record(record)


def test_issue_key_from_record_rejects_path_fragments():
    record = SourceRecord(
        id="jira://ENG/ENG-1", source_type="jira", locator="ENG-1/comment"
    )

    with pytest.raises(ValueError, match="issue key"):
        issue_key_from_record(record)


def test_build_document_metadata_handles_missing_optional_relations():
    sparse_issue = {
        "id": "1",
        "key": "ENG-1",
        "fields": {
            "summary": "Title",
            "issuelinks": [
                {
                    "id": "L1",
                    "type": {"name": "blocks"},
                    "outwardIssue": {
                        "id": "2",
                        "key": "ENG-2",
                        "fields": {
                            "summary": "out",
                            "status": {"name": "Open"},
                            "issuetype": {"name": "Task"},
                        },
                    },
                },
                {
                    "id": "L2",
                    "type": {"name": "blocked by"},
                    "inwardIssue": {
                        "id": "3",
                        "key": "ENG-3",
                        "fields": {
                            "summary": "in",
                            "status": {"name": "Open"},
                            "issuetype": {"name": "Task"},
                        },
                    },
                },
            ],
        },
    }

    metadata = build_document_metadata(sparse_issue, content="x")

    assert metadata.assignee is None
    assert metadata.reporter is None
    assert metadata.status_category is None
    assert metadata.parent is None
    assert metadata.issue_links[0].direction == "outward"
    assert metadata.issue_links[1].direction == "inward"


# ---------------------------------------------------------------------------
# utils.py edge cases
# ---------------------------------------------------------------------------


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
