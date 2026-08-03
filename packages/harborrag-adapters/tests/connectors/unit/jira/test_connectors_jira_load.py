"""Unit tests for Jira connector document loading."""

from __future__ import annotations

import json
import logging

import pytest
from jira_test_helpers import (
    CLOUD_BASE,
    FakeAttachmentParser,
    FakeJiraClient,
    cloud_config,
    issue,
)

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_load_builds_raw_document_comments_attachments_and_changelog(caplog):
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

    with caplog.at_level(logging.INFO, logger="harborrag.adapters.connectors.jira"):
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
    assert document.metadata["source_system"] == "jira"
    assert document.metadata["metadata_schema_version"] == 1
    assert document.metadata["record_id"] == "10001"
    assert document.metadata["title"] == "Build parser"
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
    assert isinstance(document.metadata["created_at"], str)
    assert isinstance(document.metadata["updated_at"], str)
    assert isinstance(document.metadata["comments"][0]["created_at"], str)
    assert isinstance(document.metadata["changelog"][0]["created_at"], str)
    json.dumps(document.metadata)  # datetimes must be JSON-serializable
    assert (
        "JIRA issue loaded issue_key=ENG-1 comments=1 attachments=1 changelog_items=1"
        in caplog.text
    )


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


def test_load_does_not_fetch_comments_or_parse_attachments_when_disabled():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    connector = JiraConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )
    record = SourceRecord(
        "jira://ENG/ENG-1",
        "jira",
        "ENG-1",
        metadata={"include_comments": False, "include_attachments": False},
    )

    document = connector.load(record)

    assert document.metadata["comments"] == []
    assert document.metadata["attachments"] == []
    assert [endpoint for endpoint, _ in client.get_calls] == ["issue/ENG-1"]


def test_load_rejects_stale_attachment_record_when_attachments_disabled():
    connector = JiraConnector(cloud_config(include_attachments=False), client=FakeJiraClient())
    record = SourceRecord(
        "jira://ENG/ENG-1/attachments/a1",
        "text/markdown",
        "a1",
        metadata={"binding_kind": "ATTACHMENT"},
    )

    with pytest.raises(DocumentProcessingError, match="attachment loading is disabled"):
        connector.load(record)


def test_load_raises_on_missing_required_fields():
    bad = issue()
    bad["fields"].pop("summary")
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", bad)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="summary"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


def test_load_rejects_issue_outside_configured_projects():
    out_of_scope = issue()
    out_of_scope["fields"]["project"] = {"key": "OPS", "name": "Operations"}
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", out_of_scope)
    connector = JiraConnector(cloud_config(project_keys=["ENG"]), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured projects"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


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
