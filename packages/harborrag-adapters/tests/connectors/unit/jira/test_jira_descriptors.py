from __future__ import annotations

from jira_test_helpers import (
    CLOUD_BASE,
    FakeJiraClient,
    cloud_config,
    issue,
)

from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_core.chunking import RelationType
from harborrag_core.domain.source import SourceRecord


def test_describe_dispatches_attachment_and_preserves_issue_relations() -> None:
    client = FakeJiraClient()
    issue_value = issue()
    issue_value["fields"]["issuelinks"] = [
        {
            "id": "link-1",
            "type": {"name": "Blocks"},
            "outwardIssue": {"key": "ENG-2"},
        }
    ]
    client.add_get("issue/ENG-1", issue_value)
    client.add_get(
        "issue/ENG-1/comment",
        {
            "comments": [
                {
                    "id": "c1",
                    "created": "2026-07-29T00:00:00Z",
                    "updated": "2026-07-30T00:00:00Z",
                }
            ],
            "total": 1,
        },
    )
    attachment_url = f"{CLOUD_BASE}/secure/attachment/a1/notes.md"
    client.downloads[attachment_url] = b"jira attachment"
    connector = JiraConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
    )
    record = SourceRecord(
        id="jira://ENG/ENG-1",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator="ENG-1",
        metadata={"include_attachments": True},
    )

    descriptor = connector.describe(record)

    assert descriptor.source.metadata["defer_attachments"] is True
    assert len(descriptor.bound_records) == 1
    assert descriptor.admission.comments[0].source_version.startswith("2026")
    assert {relation.relation_type for relation in descriptor.admission.relations} == {
        RelationType.CHILD_OF,
        RelationType.BLOCKS,
        RelationType.HAS_ATTACHMENT,
    }
    attachment = connector.load(descriptor.bound_records[0])
    assert attachment.content == b"jira attachment"
    assert attachment.metadata["relations"][0]["predicate"] == "attached_to"


def test_describe_uses_child_parent_edge_without_inverse_subtask_duplicate() -> None:
    client = FakeJiraClient()
    issue_value = issue()
    issue_value["fields"]["attachment"] = []
    issue_value["fields"]["subtasks"] = [{"id": "10002", "key": "ENG-2"}]
    client.add_get("issue/ENG-1", issue_value)
    connector = JiraConnector(
        cloud_config(include_comments=False, include_attachments=False),
        client=client,
    )

    descriptor = connector.describe(
        SourceRecord(
            id="jira://ENG/ENG-1",
            source_type="application/vnd.atlassian.jira.issue+json",
            locator="ENG-1",
        )
    )

    assert {relation.relation_type for relation in descriptor.admission.relations} == {
        RelationType.CHILD_OF,
    }
    assert {relation["predicate"] for relation in descriptor.source.metadata["relations"]} == {
        "child_of",
    }
    assert descriptor.source.metadata["subtasks"][0]["key"] == "ENG-2"


def test_describe_respects_record_attachment_and_comment_flags() -> None:
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    connector = JiraConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
    )

    descriptor = connector.describe(
        SourceRecord(
            id="jira://ENG/ENG-1",
            source_type="application/vnd.atlassian.jira.issue+json",
            locator="ENG-1",
            metadata={"include_comments": False, "include_attachments": False},
        )
    )

    assert descriptor.admission.comments == ()
    assert descriptor.admission.attachments == ()
    assert descriptor.bound_records == ()
    assert [endpoint for endpoint, _ in client.get_calls] == ["issue/ENG-1"]


def test_search_result_is_reused_for_admission_description() -> None:
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [issue()], "isLast": True})
    connector = JiraConnector(
        cloud_config(include_comments=False, include_attachments=False),
        client=client,
    )

    descriptor = connector.describe(next(connector.discover()))

    # The only GET so far is discover()'s auth preflight ("myself") --
    # describe() itself must not re-fetch the issue, reusing the search
    # result already embedded in the discovered record.
    assert client.get_calls == [("myself", None)]
    assert descriptor.admission.source_version.startswith("2024")
    assert "_jira_discovery_descriptor" not in descriptor.source.metadata
