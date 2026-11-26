"""SharePoint discovery raises on a throttled or failing listing instead of skipping it."""

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
from harborrag_adapters.connectors.exceptions import FetchError, RateLimitError
from harborrag_core.ingestion.connector_contracts import ConnectorQuery

pytestmark = pytest.mark.unit

TRANSIENT_ERRORS = [
    RateLimitError("slow down", status_code=429),
    FetchError("unavailable", status_code=503),
    FetchError("Microsoft Graph request failed: ReadTimeout"),
]


class _FailingGraphClient(FakeGraphClient):
    """Fake Graph client that raises a chosen error for chosen endpoints."""

    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, Exception] = {}

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint in self.errors:
            raise self.errors[endpoint]
        return super().get_json(endpoint, params=params)


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
def test_transient_subfolder_listing_failure_raises(error: Exception) -> None:
    client = _FailingGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), folder_item("busy", "Busy")]},
    )
    client.errors["drives/drive1/items/busy/children"] = error
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(type(error)):
        list(connector.discover())

    assert connector.skipped == ()


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
def test_transient_failure_on_a_selected_item_raises(error: Exception) -> None:
    client = _FailingGraphClient()
    add_site_and_default_drive(client)
    client.errors["drives/drive1/items/busy"] = error
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(type(error)):
        list(connector.discover(ConnectorQuery(filters={"item_ids": ["busy"]})))

    assert connector.skipped == ()
