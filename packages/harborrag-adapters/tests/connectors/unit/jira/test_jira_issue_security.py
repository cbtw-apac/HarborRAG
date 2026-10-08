"""Jira issue security levels are captured as source ACLs and fail closed."""

from __future__ import annotations

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY, SourceAcl
from harborrag_adapters.connectors.exceptions import SourceRestrictedError
from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.access import issue_acl
from harborrag_adapters.connectors.jira.issues import DESCRIPTOR_FIELDS
from harborrag_adapters.connectors.schemas import SKIP_REASON_ACCESS_RESTRICTED
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def secured(key: str = "ENG-2") -> dict:
    value = issue(key)
    value["fields"]["security"] = {"id": "10100", "name": "Internal only"}
    return value


def issue_record(key: str) -> SourceRecord:
    return SourceRecord(
        id=f"jira://ENG/{key}",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator=key,
        metadata={"issue_key": key},
    )


def test_issue_acl_maps_security_levels():
    assert issue_acl(issue()) == SourceAcl.inherited()
    assert issue_acl({**issue(), "fields": {**issue()["fields"], "security": None}}).status == (
        "inherited"
    )
    acl = issue_acl(secured())
    assert acl.status == "restricted"
    assert acl.reason == "issue_security_level"
    assert [principal.key for principal in acl.levels[0]] == ["jira_security_level:10100"]
    malformed = issue()
    malformed["fields"]["security"] = "Internal only"
    assert issue_acl(malformed).status == "unknown"


def test_security_field_is_always_requested():
    assert "security" in DESCRIPTOR_FIELDS
    explicit = cloud_config(include_all_fields=False, fields=("summary",))
    assert explicit.requested_fields() == ("summary", "security")


def test_config_rejects_unknown_restricted_content_policy():
    with pytest.raises(ValueError, match="restricted_content"):
        cloud_config(restricted_content="open")


def test_discover_withholds_secured_issues_and_reports_them():
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [issue("ENG-1"), secured("ENG-2")], "isLast": True})
    connector = JiraConnector(cloud_config(), client=client)

    records = list(connector.discover())

    assert [record.metadata["issue_key"] for record in records] == ["ENG-1"]
    assert [(skip.path, skip.reason) for skip in connector.skipped] == [
        ("ENG-2", SKIP_REASON_ACCESS_RESTRICTED)
    ]


def test_discover_page_withholds_secured_issues():
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [secured("ENG-2"), issue("ENG-3")], "isLast": True})
    connector = JiraConnector(cloud_config(), client=client)

    page = connector.discover_page(None, cursor=None, page_size=5)

    assert [record.metadata["issue_key"] for record in page.records] == ["ENG-3"]
    assert [skip.path for skip in connector.skipped] == ["ENG-2"]


def test_describe_rejects_secured_issue_under_skip_policy():
    client = FakeJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(SourceRestrictedError, match="ENG-2"):
        connector.describe(issue_record("ENG-2"))


def test_load_rejects_secured_issue_under_skip_policy():
    client = FakeJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(SourceRestrictedError, match="ENG-2"):
        connector.load(issue_record("ENG-2"))
    assert [endpoint for endpoint, _ in client.get_calls] == ["issue/ENG-2"]


def test_include_policy_keeps_secured_issue_and_labels_record_and_document():
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [secured("ENG-2")], "isLast": True})
    client.add_get("issue/ENG-2", secured("ENG-2"))
    no_comments = {"startAt": 0, "total": 0, "comments": []}
    # describe() and load() each page through the comment endpoint once.
    client.add_get("issue/ENG-2/comment", dict(no_comments), dict(no_comments))
    connector = JiraConnector(cloud_config(restricted_content="include"), client=client)

    [record] = list(connector.discover())
    descriptor = connector.describe(record)
    document = connector.load(issue_record("ENG-2"))

    assert connector.skipped == ()
    assert descriptor.source.metadata[SOURCE_ACL_METADATA_KEY]["status"] == "restricted"
    acl = document.metadata[SOURCE_ACL_METADATA_KEY]
    assert acl["levels"][0][0] == {
        "kind": "jira_security_level",
        "id": "10100",
        "display_name": "Internal only",
    }


class RecordingJiraClient(FakeJiraClient):
    def __init__(self) -> None:
        super().__init__()
        self.download_calls: list[str] = []

    def download_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes | None:
        self.download_calls.append(url)
        return super().download_bytes(url, max_bytes=max_bytes)


def attachment_record(parent_key: str = "ENG-2") -> SourceRecord:
    return SourceRecord(
        id=f"jira://ENG/{parent_key}/attachments/100001",
        source_type="text/markdown",
        locator="100001",
        metadata={
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": f"jira://ENG/{parent_key}",
            "attachment_id": "100001",
            "filename": "notes.md",
            "media_type": "text/markdown",
            "size_bytes": 5,
            "download_url": "https://example.atlassian.net/secure/attachment/100001/notes.md",
            "source_version": "1",
        },
    )


def test_attachment_load_rejects_a_parent_issue_secured_since_discovery():
    client = RecordingJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    client.downloads[attachment_record().metadata["download_url"]] = b"notes"
    connector = JiraConnector(cloud_config(include_attachments=True), client=client)

    with pytest.raises(SourceRestrictedError, match="ENG-2"):
        connector.load(attachment_record())
    assert [endpoint for endpoint, _ in client.get_calls] == ["issue/ENG-2"]
    assert client.download_calls == []


def test_attachment_load_checks_the_open_parent_with_one_descriptor_read():
    client = RecordingJiraClient()
    client.add_get("issue/ENG-2", issue("ENG-2"))
    client.downloads[attachment_record().metadata["download_url"]] = b"notes"
    connector = JiraConnector(cloud_config(include_attachments=True), client=client)

    document = connector.load(attachment_record())

    assert document.content == b"notes"
    assert [(endpoint, params) for endpoint, params in client.get_calls] == [
        ("issue/ENG-2", {"fields": ",".join(DESCRIPTOR_FIELDS)})
    ]


def test_include_policy_labels_attachment_with_the_parent_acl_read_at_load():
    client = RecordingJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    client.downloads[attachment_record().metadata["download_url"]] = b"notes"
    connector = JiraConnector(
        cloud_config(include_attachments=True, restricted_content="include"), client=client
    )

    document = connector.load(attachment_record())

    assert document.metadata[SOURCE_ACL_METADATA_KEY]["levels"][0][0]["id"] == "10100"
