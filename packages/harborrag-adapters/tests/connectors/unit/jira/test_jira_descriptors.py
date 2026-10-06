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


def _comment(comment_id: str) -> dict[str, str]:
    return {
        "id": comment_id,
        "created": "2026-07-29T00:00:00Z",
        "updated": f"2026-07-30T00:00:0{comment_id[-1]}Z",
    }


def _describe(issue_value: dict, client: FakeJiraClient, **config: object):
    client.add_get("issue/ENG-1", issue_value)
    connector = JiraConnector(
        cloud_config(include_comments=True, include_attachments=False, **config),
        client=client,
    )
    return connector.describe(
        SourceRecord(
            id="jira://ENG/ENG-1",
            source_type="application/vnd.atlassian.jira.issue+json",
            locator="ENG-1",
        )
    )


def test_describe_versions_comments_embedded_in_the_issue_without_fetching_them() -> None:
    # Search and the descriptor request both return a page of comments inline;
    # when it holds every comment, describing the issue costs no comment request.
    client = FakeJiraClient()
    issue_value = issue()
    issue_value["fields"]["comment"] = {"comments": [_comment("c1"), _comment("c2")], "total": 2}

    descriptor = _describe(issue_value, client)

    assert [comment.source_item_id for comment in descriptor.admission.comments] == ["c1", "c2"]
    assert not [endpoint for endpoint, _ in client.get_calls if endpoint.endswith("/comment")]


def test_describe_fetches_comments_when_the_embedded_page_is_partial() -> None:
    client = FakeJiraClient()
    issue_value = issue()
    issue_value["fields"]["comment"] = {"comments": [_comment("c1")], "total": 2}
    client.add_get(
        "issue/ENG-1/comment", {"comments": [_comment("c1"), _comment("c2")], "total": 2}
    )

    descriptor = _describe(issue_value, client)

    assert [comment.source_item_id for comment in descriptor.admission.comments] == ["c1", "c2"]
    assert [endpoint for endpoint, _ in client.get_calls if endpoint.endswith("/comment")] == [
        "issue/ENG-1/comment"
    ]


def test_embedded_comments_are_capped_like_fetched_ones() -> None:
    # The snapshot must not depend on which path supplied the comments.
    client = FakeJiraClient()
    issue_value = issue()
    issue_value["fields"]["comment"] = {
        "comments": [_comment("c1"), _comment("c2"), _comment("c3")],
        "total": 3,
    }

    descriptor = _describe(issue_value, client, max_comments=2)

    assert [comment.source_item_id for comment in descriptor.admission.comments] == ["c1", "c2"]
