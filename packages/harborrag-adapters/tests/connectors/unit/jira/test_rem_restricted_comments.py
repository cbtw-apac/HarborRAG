"""Restricted Jira comments stay out of everything HarborRAG indexes."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.document_transform import JiraDocumentTransform
from harborrag_adapters.connectors.jira.issues import JiraIssueAPI
from harborrag_core.domain import Document, DocumentProvenance, ParsedDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion.identity import ChangeFingerprintBuilder
from harborrag_core.ingestion.source_contracts import AdmissionSnapshot

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

RESTRICTED_TEXT = "DEMO-RESTRICTED-7f3a rollback window for the payments cluster"
PUBLIC_TEXT = "DEMO-PUBLIC-21c9 deploy runbook reviewed"
RECORD = SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1")


def _comment(comment_id: str, text: str, *, visibility: dict[str, str] | None) -> dict[str, Any]:
    comment: dict[str, Any] = {
        "id": comment_id,
        "author": {"displayName": "Demo Engineer"},
        "body": text,
        "created": "2026-01-01T00:00:00.000+0000",
        "updated": f"2026-01-02T00:00:0{comment_id[-1]}.000+0000",
    }
    if visibility is not None:
        comment["visibility"] = visibility
    return comment


RESTRICTED = _comment("c1", RESTRICTED_TEXT, visibility={"type": "role", "value": "Developers"})
PUBLIC = _comment("c2", PUBLIC_TEXT, visibility=None)


def _issue_with_comments() -> dict[str, Any]:
    value = issue()
    value["fields"]["comment"] = {"comments": [RESTRICTED, PUBLIC], "total": 2, "startAt": 0}
    value["renderedFields"] = {
        "comment": {
            "comments": [
                {"id": "c1", "body": f"<p>{RESTRICTED_TEXT}</p>"},
                {"id": "c2", "body": f"<p>{PUBLIC_TEXT}</p>"},
            ]
        }
    }
    return value


def _comment_page(*comments: dict[str, Any], start_at: int, total: int) -> dict[str, Any]:
    return {"startAt": start_at, "maxResults": 2, "total": total, "comments": list(comments)}


def _indexed_text(connector: JiraConnector) -> str:
    """Render everything one load and its canonical transform produce."""
    raw = connector.load(RECORD)
    document = Document(
        id=raw.id,
        title="ENG-1",
        content=[],
        content_type="jira_issue",
        provenance=DocumentProvenance(source="jira", record_id="ENG-1", extra=dict(raw.metadata)),
    )
    canonical = JiraDocumentTransform().transform(
        raw, ParsedDocument(content=str(raw.content), parser_name="markdown"), document
    )
    elements = [asdict(element) for element in canonical.content]
    return json.dumps(
        {
            "content": raw.content,
            "metadata": raw.metadata,
            "raw": raw.raw,
            "elements": elements,
            "extra": canonical.provenance.extra,
        },
        default=str,
    )


def test_load_leaves_a_restricted_comment_out_of_everything_indexed() -> None:
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", _issue_with_comments())
    client.add_get("issue/ENG-1/comment", _comment_page(RESTRICTED, PUBLIC, start_at=0, total=2))
    connector = JiraConnector(cloud_config(include_attachments=False), client=client)

    indexed = _indexed_text(connector)

    assert "DEMO-RESTRICTED-7f3a" not in indexed
    assert "DEMO-PUBLIC-21c9" in indexed


def test_describe_versions_only_the_comments_that_are_indexed() -> None:
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [_issue_with_comments()], "isLast": True})
    connector = JiraConnector(cloud_config(include_attachments=False), client=client)

    descriptor = connector.describe(next(connector.discover()))

    assert [item.source_item_id for item in descriptor.admission.comments] == ["c2"]
    assert "DEMO-RESTRICTED-7f3a" not in json.dumps(descriptor.source.metadata, default=str)
    assert [endpoint for endpoint, _ in client.get_calls] == ["myself"]


def test_fetch_comments_reads_past_a_page_of_only_restricted_comments() -> None:
    client = FakeJiraClient()
    second_restricted = _comment("c3", RESTRICTED_TEXT, visibility={"type": "group", "value": "x"})
    client.add_get(
        "issue/ENG-1/comment",
        _comment_page(RESTRICTED, second_restricted, start_at=0, total=3),
        _comment_page(PUBLIC, start_at=2, total=3),
    )
    api = JiraIssueAPI(client, cloud_config(page_size=2, max_comments=1))

    comments = api.fetch_comments("ENG-1")

    assert [comment["id"] for comment in comments] == ["c2"]
    assert [params["startAt"] for _, params in client.get_calls] == [0, 2]


def test_dropping_a_restricted_comment_changes_the_admission_key() -> None:
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [_issue_with_comments()], "isLast": True})
    connector = JiraConnector(cloud_config(include_attachments=False), client=client)
    upgraded = connector.describe(next(connector.discover())).admission
    indexed_before = AdmissionSnapshot(
        source_version=upgraded.source_version,
        comments=(
            *upgraded.comments,
            upgraded.comments[0].model_copy(update={"source_item_id": "c1"}),
        ),
    )
    fingerprints = ChangeFingerprintBuilder()

    before = fingerprints.admission_change_key(snapshot=indexed_before)
    after = fingerprints.admission_change_key(snapshot=upgraded)

    assert before != after
