"""Unit tests for Confluence connector metadata serialization."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from harborrag_adapters.connectors.confluence.schemas import (
    ConfluenceCommentMetadata,
    ConfluenceMetadata,
)
from harborrag_adapters.connectors.shared.attachments import AttachmentMetadata

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _metadata(**overrides) -> ConfluenceMetadata:
    values = {
        "record_id": "1",
        "content_id": "1",
        "content_type": "page",
        "title": "Page One",
        "space_key": "ENG",
        "version": 1,
        "author": "Alice",
        "created_at": datetime(2024, 1, 2, tzinfo=UTC),
        "updated_at": datetime(2024, 1, 3, tzinfo=UTC),
        "labels": [],
        "checksum": "abc",
        "comments": [],
        "attachments": [],
        "ancestors": [],
        "children": [],
        "depth": 0,
        "breadcrumb": [],
    }
    values.update(overrides)
    return ConfluenceMetadata(**values)


def test_to_dict_isoformats_top_level_datetimes():
    payload = _metadata().to_dict()
    assert payload["created_at"] == "2024-01-02T00:00:00+00:00"
    assert payload["updated_at"] == "2024-01-03T00:00:00+00:00"


def test_to_dict_isoformats_datetimes_nested_in_comments():
    payload = _metadata(
        comments=[
            ConfluenceCommentMetadata(
                id="c1",
                body="hi",
                author="Bob",
                created_at=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    ).to_dict()

    assert payload["comments"][0]["created_at"] == "2024-02-01T00:00:00+00:00"
    json.dumps(payload)  # must not raise TypeError: datetime not JSON serializable


def test_to_dict_is_json_serializable_with_nested_dataclasses():
    payload = _metadata(
        comments=[
            ConfluenceCommentMetadata(
                id="c1",
                body="hi",
                author="Bob",
                created_at=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ],
        attachments=[
            AttachmentMetadata(
                id="a1",
                title="notes.md",
                media_type="text/markdown",
                size_bytes=10,
                download_url="https://example.com/a1",
                status="processed",
                text="hello",
            )
        ],
    ).to_dict()

    json.dumps(payload)


def test_to_dict_handles_none_datetimes():
    payload = _metadata(created_at=None, updated_at=None).to_dict()
    assert payload["created_at"] is None
    assert payload["updated_at"] is None
