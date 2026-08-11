from __future__ import annotations

import pytest
from confluence_test_helpers import (
    CLOUD_BASE,
    FakeConfluenceClient,
    cloud_config,
    full_content,
)

from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.chunking import RelationType
from harborrag_core.domain.source import SourceRecord


def test_describe_dispatches_attachment_without_downloading_it() -> None:
    client = FakeConfluenceClient()
    content = full_content()
    attachment_url = f"{CLOUD_BASE}/download/attachments/1/runbook.md"
    client.add("content/1", content)
    client.add(
        "content/1/child/comment",
        {
            "results": [
                {
                    "id": "c1",
                    "version": {"number": 2},
                    "history": {},
                }
            ],
            "size": 1,
        },
    )
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "runbook.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 7},
                    "_links": {"download": "/download/attachments/1/runbook.md"},
                    "version": {"number": 4},
                }
            ],
            "size": 1,
        },
    )
    client.downloads[attachment_url] = b"runbook"
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
    )
    record = SourceRecord(
        id="confluence://ENG/1",
        source_type="text/html",
        locator="1",
        metadata={"include_attachments": True},
    )

    descriptor = connector.describe(record)

    assert descriptor.source.metadata["defer_attachments"] is True
    assert len(descriptor.bound_records) == 1
    assert descriptor.admission.comments[0].source_version == "2"
    assert {relation.relation_type for relation in descriptor.admission.relations} == {
        RelationType.CHILD_OF,
        RelationType.HAS_ATTACHMENT,
    }
    attachment = connector.load(descriptor.bound_records[0])
    assert attachment.content == b"runbook"
    assert attachment.metadata["parent_source_item_id"] == record.id


def test_describe_reuses_descriptor_fields_returned_by_search() -> None:
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {"results": [full_content()], "_links": {}},
    )
    connector = ConfluenceConnector(cloud_config(), client=client)

    record = next(connector.discover())
    descriptor = connector.describe(record)

    assert descriptor.admission.source_version == "3"
    assert [
        endpoint for endpoint, _ in client.calls if endpoint != "user/current"
    ] == ["content/search"]
    assert "_confluence_discovery_descriptor" not in descriptor.source.metadata


def test_describe_rejects_content_outside_configured_space() -> None:
    client = FakeConfluenceClient()
    content = full_content()
    content["space"] = {"key": "OTHER"}
    client.add("content/1", content)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured space"):
        connector.describe(
            SourceRecord(id="confluence://ENG/1", source_type="text/html", locator="1")
        )


def test_describe_rejects_cached_descriptor_content_outside_configured_space() -> None:
    content = full_content()
    content["space"] = {"key": "OTHER"}
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    with pytest.raises(DocumentProcessingError, match="outside configured space"):
        connector.describe(
            SourceRecord(
                id="confluence://ENG/1",
                source_type="text/html",
                locator="1",
                metadata={"_confluence_discovery_descriptor": content},
            )
        )


def test_describe_respects_record_attachment_and_comment_flags() -> None:
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
    )

    descriptor = connector.describe(
        SourceRecord(
            id="confluence://ENG/1",
            source_type="text/html",
            locator="1",
            metadata={"include_comments": False, "include_attachments": False},
        )
    )

    assert descriptor.admission.comments == ()
    assert descriptor.admission.attachments == ()
    assert descriptor.bound_records == ()
    assert [endpoint for endpoint, _ in client.calls] == ["content/1"]
