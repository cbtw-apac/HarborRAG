"""Restricted Jira comments never reach shared issue content."""

from __future__ import annotations

import json

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, dc_config, issue

from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.comment_visibility import (
    is_restricted_comment,
    redact_restricted_comments,
    shareable_comments,
)
from harborrag_adapters.connectors.jira.issues import JiraIssueAPI
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

CREATED = "2024-05-01T00:00:00.000+0000"
PUBLIC = {"id": "c1", "author": {"displayName": "Bob"}, "body": "Public note", "created": CREATED}
ROLE_ONLY = {
    "id": "c2",
    "author": {"displayName": "Eve"},
    "body": "Admins only secret",
    "visibility": {"type": "role", "value": "Administrators"},
    "created": CREATED,
}
GROUP_ONLY = {
    "id": "c3",
    "author": {"displayName": "Mal"},
    "body": "Group secret",
    "visibility": {"type": "group", "value": "jira-security"},
    "created": CREATED,
}
INTERNAL_NOTE = {
    "id": "c4",
    "author": {"displayName": "Agent"},
    "body": "Internal agent note",
    "jsdPublic": False,
    "created": CREATED,
}


# Jira Service Management Data Center marks an internal note only through this
# comment property, which the comment endpoint returns with `expand=properties`.
DC_INTERNAL_NOTE = {
    "id": "c5",
    "author": {"displayName": "Agent"},
    "body": "Data Center internal note",
    "created": CREATED,
    "properties": [{"key": "sd.public.comment", "value": {"internal": True}}],
}


def _sd_public_comment(value: object) -> dict:
    return {**PUBLIC, "properties": [{"key": "sd.public.comment", "value": value}]}


class _PropertiesAwareJiraClient(FakeJiraClient):
    """Returns comment properties only when the request expands them, as Jira does."""

    def get_json(self, endpoint, *, params=None):
        response = super().get_json(endpoint, params=params)
        expand = str((params or {}).get("expand", "")).split(",")
        if endpoint.endswith("/comment") and "properties" not in expand:
            response = {
                **response,
                "comments": [
                    {key: value for key, value in comment.items() if key != "properties"}
                    for comment in response["comments"]
                ],
            }
        return response


def test_data_center_internal_note_property_is_restricted():
    assert is_restricted_comment(DC_INTERNAL_NOTE)
    assert not is_restricted_comment(_sd_public_comment({"internal": False}))
    assert not is_restricted_comment({**PUBLIC, "properties": [{"key": "other", "value": {}}]})


@pytest.mark.parametrize("value", [{}, None, "true", {"internal": "yes"}])
def test_malformed_internal_note_property_is_restricted(value):
    assert is_restricted_comment(_sd_public_comment(value))


def test_load_omits_data_center_internal_notes_from_content_metadata_and_raw():
    client = _PropertiesAwareJiraClient()
    loaded = issue()
    # The issue's embedded comments never carry properties.
    embedded_note = {key: value for key, value in DC_INTERNAL_NOTE.items() if key != "properties"}
    loaded["fields"]["comment"] = {"total": 2, "comments": [PUBLIC, embedded_note]}
    client.add_get("issue/ENG-1", loaded)
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "total": 2, "comments": [PUBLIC, DC_INTERNAL_NOTE]},
    )
    connector = JiraConnector(dc_config(page_size=10), client=client)
    record = SourceRecord(
        id="jira://ENG/ENG-1",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator="ENG-1",
        metadata={"issue_key": "ENG-1"},
    )

    document = connector.load(record)

    text = document.text()
    assert "Public note" in text
    assert "Data Center internal note" not in text
    serialized = json.dumps(document.metadata) + json.dumps(document.raw)
    assert "Data Center internal note" not in serialized


def test_is_restricted_comment_classifies_visibility_and_internal_notes():
    assert not is_restricted_comment(PUBLIC)
    assert not is_restricted_comment({**PUBLIC, "jsdPublic": True})
    assert not is_restricted_comment({**PUBLIC, "visibility": None})
    assert is_restricted_comment(ROLE_ONLY)
    assert is_restricted_comment(GROUP_ONLY)
    assert is_restricted_comment(INTERNAL_NOTE)


def test_empty_or_malformed_visibility_is_restricted():
    assert is_restricted_comment({**PUBLIC, "visibility": {}})
    assert is_restricted_comment({**PUBLIC, "visibility": "Administrators"})


def test_shareable_comments_drops_restricted_and_non_mapping_entries():
    values = [PUBLIC, ROLE_ONLY, "junk", INTERNAL_NOTE]

    assert [comment["id"] for comment in shareable_comments(values)] == ["c1"]


def test_redact_restricted_comments_rewrites_the_embedded_page_in_place():
    payload = {"fields": {"comment": {"total": 2, "comments": [PUBLIC, GROUP_ONLY]}}}

    assert redact_restricted_comments(payload) is payload
    assert payload["fields"]["comment"]["comments"] == [PUBLIC]
    assert redact_restricted_comments({"fields": {}}) == {"fields": {}}


def test_redact_filters_rendered_comments_by_shareable_id():
    payload = {
        "fields": {"comment": {"total": 2, "comments": [PUBLIC, GROUP_ONLY]}},
        "renderedFields": {
            "description": "<p>kept</p>",
            "comment": {
                "comments": [
                    {"id": "c1", "body": "<p>Public</p>"},
                    {"id": "c3", "body": "<p>Group secret</p>"},
                ]
            },
        },
    }

    redact_restricted_comments(payload)

    assert payload["renderedFields"]["comment"]["comments"] == [
        {"id": "c1", "body": "<p>Public</p>"}
    ]
    assert payload["renderedFields"]["description"] == "<p>kept</p>"


def test_redact_drops_rendered_comments_it_cannot_verify():
    payload = {
        "fields": {},
        "renderedFields": {"comment": {"comments": [{"id": "c3", "body": "x"}]}},
    }

    redact_restricted_comments(payload)

    assert "comment" not in payload["renderedFields"]


PUBLIC_WORKLOG = {"id": "w1", "comment": "Public work", "timeSpent": "1h"}
GROUP_WORKLOG = {
    "id": "w2",
    "comment": "Group-only work",
    "timeSpent": "2h",
    "visibility": {"type": "group", "value": "group-a"},
}


def test_redact_drops_restricted_worklogs_and_their_rendered_copies():
    payload = {
        "fields": {"worklog": {"total": 2, "worklogs": [PUBLIC_WORKLOG, GROUP_WORKLOG]}},
        "renderedFields": {
            "worklog": {
                "worklogs": [
                    {"id": "w1", "comment": "<p>Public work</p>"},
                    {"id": "w2", "comment": "<p>Group-only work</p>"},
                ]
            }
        },
    }

    redact_restricted_comments(payload)

    assert payload["fields"]["worklog"]["worklogs"] == [PUBLIC_WORKLOG]
    assert payload["renderedFields"]["worklog"]["worklogs"] == [
        {"id": "w1", "comment": "<p>Public work</p>"}
    ]


def test_redact_drops_rendered_worklogs_it_cannot_verify():
    payload = {
        "fields": {},
        "renderedFields": {"worklog": {"worklogs": [{"id": "w2", "comment": "x"}]}},
    }

    redact_restricted_comments(payload)

    assert "worklog" not in payload["renderedFields"]


def test_fetch_comments_excludes_restricted_comments():
    client = FakeJiraClient()
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "total": 3, "comments": [PUBLIC, ROLE_ONLY, GROUP_ONLY]},
    )
    api = JiraIssueAPI(client, cloud_config(page_size=5))

    assert [comment["id"] for comment in api.fetch_comments("ENG-1")] == ["c1"]


def test_issue_comments_filters_a_complete_embedded_page_without_refetching():
    client = FakeJiraClient()
    api = JiraIssueAPI(client, cloud_config())
    payload = {"fields": {"comment": {"total": 2, "comments": [PUBLIC, INTERNAL_NOTE]}}}

    assert [comment["id"] for comment in api.issue_comments("ENG-1", payload)] == ["c1"]
    assert client.get_calls == []


def test_search_page_redacts_restricted_embedded_comments():
    client = FakeJiraClient()
    found = issue("ENG-1")
    found["fields"]["comment"] = {"total": 2, "comments": [PUBLIC, ROLE_ONLY]}
    client.add_post("search/jql", {"issues": [found], "isLast": True})
    api = JiraIssueAPI(client, cloud_config())

    issues, _ = api.search_page("project = ENG", cursor=None, limit=5)

    assert [comment["id"] for comment in issues[0]["fields"]["comment"]["comments"]] == ["c1"]


def test_load_omits_restricted_comment_text_from_content_metadata_and_raw():
    client = FakeJiraClient()
    loaded = issue()
    loaded["fields"]["comment"] = {"total": 2, "comments": [PUBLIC, ROLE_ONLY]}
    loaded["renderedFields"] = {
        "comment": {
            "comments": [
                {"id": "c1", "body": "<p>Public note</p>"},
                {"id": "c2", "body": "<p>Admins only secret</p>"},
            ]
        }
    }
    client.add_get("issue/ENG-1", loaded)
    client.add_get(
        "issue/ENG-1/comment",
        {"startAt": 0, "total": 4, "comments": [PUBLIC, ROLE_ONLY, GROUP_ONLY, INTERNAL_NOTE]},
    )
    connector = JiraConnector(cloud_config(page_size=10), client=client)
    record = SourceRecord(
        id="jira://ENG/ENG-1",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator="ENG-1",
        metadata={"issue_key": "ENG-1"},
    )

    document = connector.load(record)

    text = document.text()
    assert "Public note" in text
    serialized = json.dumps(document.metadata) + json.dumps(document.raw)
    for secret in ("Admins only secret", "Group secret", "Internal agent note"):
        assert secret not in text
        assert secret not in serialized
