"""SharePoint load failures keep the classification the engine's failure policy expects."""

from __future__ import annotations

from typing import Any

import pytest
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    folder_item,
)

from harborrag_adapters.connectors import SharePointConnector
from harborrag_adapters.connectors.exceptions import FetchError, ItemNotFoundError, RateLimitError
from harborrag_adapters.connectors.sharepoint.drive_paths import item_path
from harborrag_adapters.connectors.sharepoint.skips import (
    SharePointItemMissingError,
    SharePointItemUnavailableError,
)
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    SourceItemNotFoundError,
    SourceUnavailableError,
    UnsupportedDocumentError,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class _RaisingClient(FakeGraphClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint == "drives/drive1/items/file1":
            raise self.error
        return super().get_json(endpoint, params=params)


def _load(error: Exception) -> BaseException:
    client = _RaisingClient(error)
    add_site_and_default_drive(client)
    connector = SharePointConnector(config(), client=client)
    record = SourceRecord(
        "sharepoint://site1/drive1/file1",
        "application/octet-stream",
        "file1",
        metadata={"drive_id": "drive1", "item_id": "file1"},
    )
    with pytest.raises(Exception) as excinfo:
        connector.load(record)
    return excinfo.value


def test_a_deleted_item_fails_as_a_missing_source_item_not_unsupported():
    error = _load(FetchError("gone", status_code=404))

    assert isinstance(error, SharePointItemMissingError)
    assert isinstance(error, ItemNotFoundError)
    assert isinstance(error, SourceItemNotFoundError)
    assert not isinstance(error, UnsupportedDocumentError)
    assert not error.retryable
    assert (error.skip_reason, error.item_id, error.status_code) == ("DELETED", "file1", 404)


@pytest.mark.parametrize(
    "graph_error",
    [RateLimitError("throttled", status_code=429), FetchError("down", status_code=503)],
)
def test_exhausted_retries_stay_a_retryable_source_unavailable_failure(graph_error):
    error = _load(graph_error)

    assert isinstance(error, SharePointItemUnavailableError)
    assert isinstance(error, SourceUnavailableError)
    assert not isinstance(error, (UnsupportedDocumentError, SourceItemNotFoundError))
    assert error.retryable
    assert error.skip_reason == "TRANSIENT_ERROR_MAX_RETRIES"
    assert error.status_code == graph_error.status_code
    assert error.item_id == "file1"


def test_item_path_percent_decodes_the_graph_parent_path():
    item = {"name": "a.docx", "parentReference": {"path": "/drives/b!x/root:/HR%20Policies"}}
    nested = {
        "name": "b.docx",
        "parentReference": {"path": "/drive/root:/T%C3%A0i%20li%E1%BB%87u/Q1"},
    }

    assert item_path(item) == "HR Policies/a.docx"
    assert item_path(nested) == "Tài liệu/Q1/b.docx"


@pytest.mark.parametrize("folder", ["HR Policies", "Tài liệu"])
def test_path_allowlist_matches_folders_graph_returns_percent_encoded(folder):
    from urllib.parse import quote

    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [folder_item("hr", folder)]})
    nested = folder_item("sub", "Sub")
    nested["parentReference"] = {
        "driveId": "drive1",
        "id": "hr",
        "path": f"/drive/root:/{quote(folder)}",
    }
    child = file_item("file1", "a.docx", parent_id="hr")
    child["parentReference"]["path"] = f"/drive/root:/{quote(folder)}"
    client.add("drives/drive1/items/hr/children", {"value": [child, nested]})
    deep = file_item("file2", "b.docx", parent_id="sub")
    deep["parentReference"]["path"] = f"/drive/root:/{quote(folder)}/Sub"
    client.add("drives/drive1/items/sub/children", {"value": [deep]})
    connector = SharePointConnector(config(allowed_path_prefixes=[folder]), client=client)

    records = list(connector.discover())

    assert [record.locator for record in records] == ["file1", "file2"]
    assert connector.skipped == ()


def test_sharepoint_streams_discovery_instead_of_offset_replay_paging():
    # The inherited discover_page re-walks the drive from the start for every
    # page; without a native cursor the runtime must stream discover() once.
    assert SharePointConnector.capabilities.pagination is False
