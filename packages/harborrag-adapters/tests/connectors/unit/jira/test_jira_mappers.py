"""Unit tests for Jira connector record/metadata mapping helpers."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.jira.mappers import (
    build_document_metadata,
    build_source_record,
    issue_key_from_record,
    parse_timestamp,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_parse_timestamp_returns_none_for_invalid_string():
    assert parse_timestamp("not-a-timestamp") is None


def test_issue_key_from_record_requires_issue_key():
    record = SourceRecord(id="jira://x", source_type="jira", locator="")

    with pytest.raises(ValueError, match="does not contain issue_key"):
        issue_key_from_record(record)


def test_issue_key_from_record_rejects_path_fragments():
    record = SourceRecord(id="jira://ENG/ENG-1", source_type="jira", locator="ENG-1/comment")

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


def _related(key: str, issue_id: str) -> dict:
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "summary": f"Sample Summary {key}",
            "status": {"name": "Open"},
            "issuetype": {"name": "Task"},
        },
    }


def test_related_issues_carry_only_their_key_and_id():
    """Parent, subtask and linked issues may be withheld, so their prose is not copied."""

    issue = {
        "id": "1",
        "key": "ENG-1",
        "fields": {
            "summary": "Visible",
            "parent": _related("ENG-2", "2"),
            "subtasks": [_related("ENG-3", "3")],
            "issuelinks": [
                {"id": "L1", "type": {"name": "blocks"}, "outwardIssue": _related("ENG-4", "4")}
            ],
        },
    }

    metadata = build_document_metadata(issue, content="x").to_dict()
    record = build_source_record(issue, base_url="https://example.atlassian.net")

    assert metadata["parent"] == {"id": "2", "key": "ENG-2"}
    assert metadata["subtasks"] == [{"id": "3", "key": "ENG-3"}]
    assert metadata["issue_links"][0]["issue"] == {"id": "4", "key": "ENG-4"}
    assert record.metadata["subtasks"] == [{"id": "3", "key": "ENG-3"}]
    assert "Sample Summary" not in repr((metadata, record.metadata))
