"""Whitebox unit tests for SharePointDriveAPI.should_process_file and size limits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.drive import SharePointDriveAPI
from sharepoint_test_helpers import FakeGraphClient, config, file_item

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_should_process_file_hidden_allowed_when_include_hidden():
    api = SharePointDriveAPI(FakeGraphClient(), config(include_hidden=True))
    item = file_item("file1", "Guide.docx", hidden=True)
    assert api.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_excludes_disallowed_extension():
    api = SharePointDriveAPI(FakeGraphClient(), config(allowed_extensions={".pdf"}))
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_callback_reject_allow_and_exception_paths():
    calls: list[str] = []

    def reject(name, size, mime):
        calls.append(name)
        return False, "policy"

    api = SharePointDriveAPI(FakeGraphClient(), config(process_file_callback=reject))
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False
    assert calls == ["Guide.docx"]

    def explode(name, size, mime):
        raise RuntimeError("boom")

    api_swallow = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=explode, fail_on_error=False)
    )
    assert api_swallow.should_process_file(item, ConnectorQuery()) is False

    api_raise = SharePointDriveAPI(
        FakeGraphClient(), config(process_file_callback=explode, fail_on_error=True)
    )
    with pytest.raises(RuntimeError):
        api_raise.should_process_file(item, ConnectorQuery())

    def allow(name, size, mime):
        return True, ""

    api_allow = SharePointDriveAPI(FakeGraphClient(), config(process_file_callback=allow))
    assert api_allow.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_no_size_limit_configured():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=None))
    item = file_item("file1", "Guide.docx", size=10_000_000)
    assert api.should_process_file(item, ConnectorQuery()) is True


def test_should_process_file_rejects_oversized_file():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    item = file_item("file1", "Guide.docx", size=100)
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_excludes_matching_extension():
    api = SharePointDriveAPI(FakeGraphClient(), config(excluded_extensions={".docx"}))
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery()) is False


def test_should_process_file_skips_when_older_than_updated_after():
    api = SharePointDriveAPI(FakeGraphClient(), config())
    item = file_item("file1", "Guide.docx", updated="2020-01-01T00:00:00Z")
    query = ConnectorQuery(updated_after=datetime(2024, 1, 1, tzinfo=UTC))
    assert api.should_process_file(item, query) is False


def test_should_process_file_rejects_pattern_mismatch():
    api = SharePointDriveAPI(FakeGraphClient(), config())
    item = file_item("file1", "Guide.docx")
    assert api.should_process_file(item, ConnectorQuery(pattern="*.pdf")) is False


def test_enforce_size_limit_noop_when_no_limit_configured():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=None))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=10_000_000))


def test_enforce_size_limit_noop_when_size_is_zero():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=0))


def test_enforce_size_limit_raises_when_exceeded():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=5))
    with pytest.raises(DocumentProcessingError, match="exceeds"):
        api.enforce_size_limit(file_item("file1", "Guide.docx", size=100))


def test_enforce_size_limit_allows_within_bounds():
    api = SharePointDriveAPI(FakeGraphClient(), config(max_file_size_bytes=100))
    api.enforce_size_limit(file_item("file1", "Guide.docx", size=10))
