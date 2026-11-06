"""Jira output keeps its document identity and change-detection values."""

from __future__ import annotations

import pytest
from jira_test_helpers import CLOUD_BASE, FakeJiraClient, cloud_config, issue

from harborrag_adapters.connectors.jira import JiraConnector
from harborrag_core.chunking import ConnectorType

pytestmark = pytest.mark.unit

PINS: dict[str, tuple[str, str, str, str]] = {
    "jira://ENG/ENG-1": (
        "document:b3eed87c3ea9fabf3ef1b9c30b7db910f3d2e45f2ba45514262a12092b9f4d54",
        "admission-change:8625822ff46ec0700cc36d42f690284e0ebae02be9e1a3a9b8fcec4febfae1e5",
        "d2a5b02938c61d18073156e9c156f4dc832333a4ef8ac06cb13a4a29481e141e",
        "retrieval-metadata:dec90725ce6099490ea30343535ad6a0f27b99bc8d908cf8b841475a7d592efe",
    ),
    "jira://ENG/ENG-1/attachments/a1": (
        "document:8becf212ea6cc27c838946ac3a8b1ae08854e26efe6580ca488c47c510cdfd94",
        "admission-change:2d75136660a117956d61fc286e8570e36eb988c5dff6fa020fb42d893dfe29dc",
        "409bcdc9ba64779508effe60eb5fcd69b5a65c5b8bdd0b2be24bbaa0a0fbcf66",
        "retrieval-metadata:89034c18d42e636ab5138318d28ca95685ac90cb45cfe6bc6111e9da155ed76d",
    ),
}


def test_jira_issue_and_attachment_keep_their_identity_and_hashes(pin_connector) -> None:
    client = FakeJiraClient()
    client.add_post("search/jql", {"issues": [issue("ENG-1")], "isLast": True})
    # describe() reads the issue for its admission key, then load() reads it again.
    for _ in range(2):
        _add_issue(client)
    client.downloads[f"{CLOUD_BASE}/secure/attachment/a1/notes.md"] = b"# Notes\n\nShip it."
    connector = JiraConnector(
        cloud_config(include_attachments=True, include_changelog=True), client=client
    )

    assert pin_connector(connector, connector_type=ConnectorType.JIRA) == PINS


def _add_issue(client: FakeJiraClient) -> None:
    client.add_get("issue/ENG-1", issue("ENG-1"))
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
                    "items": [{"field": "status", "fromString": "Open", "toString": "Done"}],
                }
            ],
        },
    )
