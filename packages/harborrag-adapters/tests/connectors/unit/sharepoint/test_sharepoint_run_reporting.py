"""Unit tests for SharePoint run IDs, structured logs, startup auth, and author metadata."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    drive,
    file_item,
    site,
)

from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.exceptions import AuthenticationError, AuthorizationError
from harborrag_adapters.connectors.sharepoint.mappers import (
    build_document_metadata,
    build_source_record,
    parse_timestamp,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

_LOGGER = "harborrag.adapters.connectors.sharepoint"


class _FailingSiteClient(FakeGraphClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def get_json(self, endpoint, *, params=None):
        if endpoint == "sites/site1":
            self.calls.append((endpoint, params))
            raise self.error
        return super().get_json(endpoint, params=params)


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(record.getMessage()) for record in caplog.records if record.name == _LOGGER]


def _discover_with_one_skip(connector: SharePointConnector, client: FakeGraphClient) -> None:
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root:/Docs:/children",
        {"value": [file_item("file1", "Guide.docx"), file_item("big", "Big.docx", size=99)]},
    )
    list(connector.discover())


def test_skip_and_summary_logs_are_json_tagged_with_the_bound_run_id(caplog):
    client = FakeGraphClient()
    connector = SharePointConnector(config(root_path="Docs", max_file_size_bytes=50), client=client)
    connector.bind_run_id("task-42")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _discover_with_one_skip(connector, client)

    events = _events(caplog)
    skip = next(event for event in events if event["event"] == "sharepoint.item_skipped")
    assert skip["run_id"] == "task-42"
    assert skip["skip_reason"] == "EXCEEDS_SIZE_LIMIT"
    assert skip["item_id"] == "big"
    assert (
        skip["url"]
        == "https://contoso.sharepoint.com/sites/Engineering/Shared%20Documents/Big.docx"
    )
    summary = next(event for event in events if event["event"] == "sharepoint.discovery_summary")
    assert summary == {
        "event": "sharepoint.discovery_summary",
        "run_id": "task-42",
        "discovered": 1,
        "skipped": 1,
        "skipped_by_reason": {"EXCEEDS_SIZE_LIMIT": 1},
    }


def test_each_connector_gets_its_own_run_id_until_one_is_bound():
    first = SharePointConnector(config(), client=FakeGraphClient())
    second = SharePointConnector(config(), client=FakeGraphClient())

    assert first.run_id and second.run_id
    assert first.run_id != second.run_id


def _site_id_config() -> SharePointSiteConfig:
    return SharePointSiteConfig(
        site_id="site1", drive_id="drive1", access_token="token", requests_per_minute=6000
    )


def test_connect_reads_a_site_configured_by_id_so_auth_fails_at_startup():
    client = _FailingSiteClient(AuthenticationError("bad credentials", status_code=401))
    connector = SharePointConnector(_site_id_config(), client=client)

    with pytest.raises(AuthenticationError):
        connector.connect()
    assert client.calls[0][0] == "sites/site1"


def test_connect_reports_a_missing_sites_selected_grant_for_a_site_id():
    client = _FailingSiteClient(AuthorizationError("forbidden", status_code=403))
    connector = SharePointConnector(_site_id_config(), client=client)

    with pytest.raises(AuthorizationError, match="Sites.Selected"):
        connector.connect()


def test_discovery_checks_access_before_walking_a_site_configured_by_id():
    client = _FailingSiteClient(AuthenticationError("bad credentials", status_code=401))
    connector = SharePointConnector(_site_id_config(), client=client)

    with pytest.raises(AuthenticationError):
        list(connector.discover())
    assert [endpoint for endpoint, _ in client.calls] == ["sites/site1"]


def test_author_prefers_the_user_over_the_application_in_an_identity_set():
    item = file_item()
    item["createdBy"] = {
        "application": {"displayName": "Migration Tool"},
        "user": {"displayName": "Ada"},
    }

    record = build_source_record(item, site_id="site1", drive_id="drive1")
    metadata = build_document_metadata(item, site=site(), drive=drive(), checksum="sum")

    assert record.metadata["author"] == "Ada"
    assert metadata.author == "Ada"


def test_timestamps_are_normalized_to_utc():
    assert parse_timestamp("2024-05-24T22:57:56+02:00") == datetime(
        2024, 5, 24, 20, 57, 56, tzinfo=UTC
    )
    assert parse_timestamp("2024-05-24T20:57:56Z").tzinfo is UTC
