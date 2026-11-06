"""Confluence output keeps its document identity and change-detection values."""

from __future__ import annotations

import pytest
from confluence_test_helpers import CLOUD_BASE, FakeConfluenceClient, cloud_config, full_content

from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_core.chunking import ConnectorType

pytestmark = pytest.mark.unit

PINS: dict[str, tuple[str, str, str, str]] = {
    "confluence://ENG/1": (
        "document:e2fb8489cccae293e31c9cdd81f8841b8951f28540e2e07713155babe8df5d02",
        "admission-change:b4911bd29c37c8a9b4e774468ad343227b1b417c25b5e668d5d6abb3d52a8fa2",
        "e926b06b8caecc13daa44a637c84ed02fd7694cad776dbe9dbf8032408a7d292",
        "retrieval-metadata:17c0609bea463ecf330b586e38e7b90ae3d5bc9c9dcc472edbd4cfefb184e273",
    ),
    "confluence://ENG/1/attachments/a1": (
        "document:763084192a526eb9438bc023de247a0b36f46e23b237d8ba0d4515d44acb9b84",
        "admission-change:ca536704b52d20f75381be6050e1b2de882c60ccddb6454e8e2a3cd6b2c4ab39",
        "409bcdc9ba64779508effe60eb5fcd69b5a65c5b8bdd0b2be24bbaa0a0fbcf66",
        "retrieval-metadata:150b3c521d0341d482ccdf5fdff77cecb3a4f3a314e138a95e45ba5877c76262",
    ),
}

STORAGE = (
    "<h1>Deploy</h1><p>Run the <strong>pipeline</strong>.</p>"
    '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>Check the alarms.</p>'
    "</ac:rich-text-body></ac:structured-macro>"
    "<h2>Hosts</h2><table><tbody><tr><th>Host</th><th>Role</th></tr>"
    "<tr><td>web-1</td><td>frontend</td></tr><tr><td>db-1</td><td>database</td></tr>"
    "</tbody></table>"
    '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>'
    "<ac:plain-text-body><![CDATA[make deploy]]></ac:plain-text-body></ac:structured-macro>"
    '<p>See <ac:link><ri:page ri:content-title="Runbook" /></ac:link>.</p>'
)


def test_confluence_page_and_attachment_keep_their_identity_and_hashes(pin_connector) -> None:
    client = FakeConfluenceClient()
    client.add("content/search", {"results": [_page()], "_links": {}})
    # Each load resolves the page link by title.
    client.add("content/search", *([{"results": [_runbook()], "_links": {}}] * 3))
    # describe() reads the page for its admission key, then load() reads it again.
    for _ in range(3):
        client.add("content/1", _page())
        client.add(
            "content/1/child/comment",
            {
                "results": [
                    {
                        "id": "c1",
                        "body": {"storage": {"value": "<p>Nice</p>"}},
                        "history": {"createdBy": {"displayName": "Carol"}},
                        "version": {"number": 1},
                    }
                ]
            },
        )
        client.add(
            "content/1/child/attachment",
            {
                "results": [
                    {
                        "id": "a1",
                        "title": "notes.md",
                        "metadata": {"mediaType": "text/markdown"},
                        "extensions": {"fileSize": 16},
                        "version": {"number": 2},
                        "_links": {"download": "/download/a1"},
                    }
                ]
            },
        )
    client.downloads[f"{CLOUD_BASE}/download/a1"] = b"# Notes\n\nShip it."
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True), client=client
    )

    assert pin_connector(connector, connector_type=ConnectorType.CONFLUENCE) == PINS


def _page() -> dict:
    page = full_content("1")
    page["body"] = {"storage": {"value": STORAGE}, "export_view": {"value": STORAGE}}
    return page


def _runbook() -> dict:
    return {"id": "77", "title": "Runbook", "type": "page", "space": {"key": "ENG"}}
