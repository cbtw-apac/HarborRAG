"""Unit tests for SharePoint connector record/metadata mapping helpers."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors.sharepoint.mappers import (
    build_document_metadata,
    drive_item_id_from_record,
)
from harborrag_adapters.connectors.sharepoint.mappers import (
    parse_timestamp as mapper_parse_timestamp,
)
from harborrag_core.domain.source import SourceRecord
from sharepoint_test_helpers import drive, site

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_parse_timestamp_handles_missing_and_invalid_values():
    assert mapper_parse_timestamp(None) is None
    assert mapper_parse_timestamp("not-a-timestamp") is None


def test_drive_item_id_from_record_requires_an_item_id():
    record = SourceRecord("sharepoint://site1/drive1/x", "application/octet-stream", "")
    record.metadata.pop("item_id", None)
    with pytest.raises(ValueError, match="does not contain item_id"):
        drive_item_id_from_record(record)


def test_build_document_metadata_handles_missing_file_and_identity_info():
    item = {
        "id": "file1",
        "name": "Notes.txt",
        "parentReference": {},
        "createdBy": {"nonDictValue": "oops", "application": {"id": "app-1"}},
        "lastModifiedBy": "not-a-dict",
    }
    metadata = build_document_metadata(item, site=site(), drive=drive(), checksum="etag-1")
    assert metadata.sharepoint_hashes == {}
    assert metadata.created_by is None
    assert metadata.updated_by is None
