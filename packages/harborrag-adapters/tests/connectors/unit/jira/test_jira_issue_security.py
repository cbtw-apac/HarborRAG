"""Jira issue security levels are captured as source ACLs and fail closed."""

from __future__ import annotations

import pytest
from jira_test_helpers import FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY, SourceAcl, SourcePrincipal
from harborrag_adapters.connectors.exceptions import SourceRestrictedError
from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_adapters.connectors.jira.access import issue_acl
from harborrag_adapters.connectors.jira.issues import DESCRIPTOR_FIELDS
from harborrag_adapters.connectors.schemas import SKIP_REASON_ACCESS_RESTRICTED
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

R = "atlassian:acme.atlassian.net"
ENG_REALM = "atlassian:example.atlassian.net"


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
    assert issue_acl(issue(), realm=ENG_REALM) == SourceAcl.inherited(
        realm=ENG_REALM, scope="jira-project:ENG"
    )
    open_issue = {**issue(), "fields": {**issue()["fields"], "security": None}}
    assert issue_acl(open_issue, realm=ENG_REALM).status == "inherited"
    acl = issue_acl(secured(), realm=ENG_REALM)
    assert acl.status == "restricted"
    assert acl.reason == "issue_security_level"
    assert acl.realm == ENG_REALM
    assert acl.scope == "jira-project:ENG"
    assert [principal.key for principal in acl.levels[0]] == [
        f"{ENG_REALM}|jira_security_level:ENG/10100"
    ]
    malformed = issue()
    malformed["fields"]["security"] = "Internal only"
    unreadable = issue_acl(malformed, realm=ENG_REALM)
    assert unreadable.status == "unknown"
    assert unreadable.reason == "issue_security_level_unreadable"


def test_atlassian_realm_is_not_hardwired_into_issue_acl():
    other = "atlassian:other.atlassian.net"

    assert issue_acl(issue(), realm=other).realm == other
    assert issue_acl(secured(), realm=other).levels[0][0].realm == other


def test_issue_without_security_inherits_its_project():
    value = {"fields": {"project": {"key": "CPM"}, "security": None}}

    assert issue_acl(value, realm=R) == SourceAcl.inherited(realm=R, scope="jira-project:CPM")


def test_security_level_is_a_realm_principal():
    value = {"fields": {"project": {"key": "CPM"}, "security": {"id": "10001", "name": "HR"}}}

    acl = issue_acl(value, realm=R)

    assert acl.scope == "jira-project:CPM"
    assert acl.levels == ((SourcePrincipal(realm=R, kind="jira_security_level", id="CPM/10001"),),)
    assert acl.levels[0][0].display_name == "HR"


def test_a_security_level_is_named_within_the_issues_project():
    # One security scheme can serve several projects, and its project-role and project-lead
    # members resolve in the issue's own project, so the level id alone names no one.
    level = {"id": "10001", "name": "HR"}
    cpm = issue_acl({"fields": {"project": {"key": "CPM"}, "security": level}}, realm=R)
    ops = issue_acl({"fields": {"project": {"key": " OPS "}, "security": level}}, realm=R)

    (cpm_level,) = cpm.levels[0]
    (ops_level,) = ops.levels[0]
    assert (cpm_level.kind, cpm_level.id) == ("jira_security_level", "CPM/10001")
    assert (ops_level.kind, ops_level.id) == ("jira_security_level", "OPS/10001")
    assert cpm_level != ops_level
    assert cpm_level.key == f"{R}|jira_security_level:CPM/10001"
    # The level is named by the project key its scope names, and the two classes differ:
    # the engine fingerprints exactly this canonical form.
    assert cpm.scope == "jira-project:CPM"
    assert ops.scope == "jira-project:OPS"
    assert cpm.canonical() != ops.canonical()


def test_issue_without_project_is_unknown():
    acl = issue_acl({"fields": {"security": None}}, realm=R)

    assert acl.status == "unknown"
    assert acl.reason == "issue_project_unreadable"


@pytest.mark.parametrize(
    "project",
    [None, "CPM", {}, {"key": ""}, {"key": "  "}, {"key": 7}, {"key": ["CPM"]}],
)
def test_issue_with_an_unreadable_project_is_unknown_even_when_it_has_a_security_level(project):
    value = {"fields": {"project": project, "security": {"id": "10001", "name": "HR"}}}

    acl = issue_acl(value, realm=R)

    assert acl.status == "unknown"
    assert acl.reason == "issue_project_unreadable"


def test_issue_without_fields_is_unknown():
    assert issue_acl({}, realm=R).status == "unknown"
    assert issue_acl({"fields": None}, realm=R).status == "unknown"


def test_security_field_is_always_requested():
    assert "security" in DESCRIPTOR_FIELDS
    explicit = cloud_config(include_all_fields=False, fields=("summary",))
    assert explicit.requested_fields() == ("summary", "security", "project")


def test_project_field_is_always_requested_because_the_acl_scope_comes_from_it():
    assert "project" in DESCRIPTOR_FIELDS
    explicit = cloud_config(include_all_fields=False, fields=("summary", "project"))
    assert explicit.requested_fields() == ("summary", "project", "security")


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
        "realm": ENG_REALM,
        "kind": "jira_security_level",
        "id": "ENG/10100",
        "display_name": "Internal only",
    }


def test_open_issue_records_its_inherited_acl_on_the_record_descriptor_and_document():
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [issue("ENG-1")], "isLast": True})
    client.add_get("issue/ENG-1", issue("ENG-1"))
    connector = JiraConnector(cloud_config(include_comments=False), client=client)

    [record] = list(connector.discover())
    descriptor = connector.describe(record)
    document = connector.load(issue_record("ENG-1"))

    expected = {
        "realm": ENG_REALM,
        "scope": "jira-project:ENG",
        "status": "inherited",
        "reason": "",
        "levels": [],
    }
    assert SOURCE_ACL_METADATA_KEY not in record.metadata
    assert descriptor.source.metadata[SOURCE_ACL_METADATA_KEY] == expected
    assert document.metadata[SOURCE_ACL_METADATA_KEY] == expected


def test_secured_issue_acl_carries_the_connector_realm_and_project_scope():
    client = FakeJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    connector = JiraConnector(
        cloud_config(include_comments=False, restricted_content="include"), client=client
    )

    document = connector.load(issue_record("ENG-2"))

    acl = document.metadata[SOURCE_ACL_METADATA_KEY]
    assert (acl["realm"], acl["scope"]) == (ENG_REALM, "jira-project:ENG")
    assert acl["levels"][0][0]["realm"] == ENG_REALM


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
    acl = document.metadata[SOURCE_ACL_METADATA_KEY]
    assert (acl["status"], acl["realm"], acl["scope"]) == (
        "inherited",
        ENG_REALM,
        "jira-project:ENG",
    )


def test_include_policy_labels_attachment_with_the_parent_acl_read_at_load():
    client = RecordingJiraClient()
    client.add_get("issue/ENG-2", secured("ENG-2"))
    client.downloads[attachment_record().metadata["download_url"]] = b"notes"
    connector = JiraConnector(
        cloud_config(include_attachments=True, restricted_content="include"), client=client
    )

    document = connector.load(attachment_record())

    assert document.metadata[SOURCE_ACL_METADATA_KEY]["levels"][0][0]["id"] == "ENG/10100"
