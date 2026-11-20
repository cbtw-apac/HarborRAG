"""Regression: allowed_path_prefixes match the folders a user sees in SharePoint."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest
from sharepoint_test_helpers import (
    SITE_URL,
    FakeGraphClient,
    config,
    file_item,
    folder_item,
    site,
)

from harborrag_adapters.connectors import SharePointConnector

pytestmark = pytest.mark.unit

LIBRARY = {
    "id": "drive1",
    "name": "Documents",
    "driveType": "documentLibrary",
    "webUrl": f"{SITE_URL}/Shared%20Documents",
}


def _client_with_one_file(folder_name: str) -> FakeGraphClient:
    """Return a Graph client whose library holds one file in one top-level folder."""
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    child = file_item("f1", "Design review.docx", parent_id="d1")
    child["parentReference"]["path"] = f"/drive/root:/{quote(folder_name)}"
    client.add("drives/drive1/root/children", {"value": [folder_item("d1", folder_name)]})
    client.add("drives/drive1/items/d1/children", {"value": [child]})
    return client


def _discover(client: FakeGraphClient, **overrides: Any) -> list[str]:
    connector = SharePointConnector(config(**overrides), client=client)
    return [record.locator for record in connector.discover()]


def _discover_under(folder_name: str, prefix: str) -> list[str]:
    """Discover one file in a top-level folder of the default library."""
    client = _client_with_one_file(folder_name)
    client.add("sites/site1/drive", LIBRARY)
    return _discover(client, allowed_path_prefixes=[prefix])


@pytest.mark.parametrize(
    ("folder_name", "prefix"),
    [
        ("Q3\u00a0Plans", "Q3 Plans"),
        ("Q3\u202fPlans", "Q3 Plans"),
        ("Q3 Plans", "Q3\u00a0Plans"),
        ("Q3 Plans", "q3 plans"),
    ],
)
def test_sharepoint_prefix_matches_a_folder_whatever_its_space_characters(
    folder_name: str, prefix: str
) -> None:
    """A no-break space and a normal space match, as upper and lower case already do."""
    assert _discover_under(folder_name, prefix) == ["f1"]


@pytest.mark.parametrize("prefix", ["Shared Documents/Q3 Plans", "Documents/Q3 Plans"])
def test_sharepoint_prefix_may_start_with_the_library_name(prefix: str) -> None:
    """A prefix written from the library down matches the same folder inside it."""
    assert _discover_under("Q3 Plans", prefix) == ["f1"]


@pytest.mark.parametrize("prefix", ["Documents", "Shared Documents"])
def test_sharepoint_bare_library_name_is_not_library_wide_access(prefix: str) -> None:
    """A prefix that is only the library's name still names a folder, never the whole library."""
    assert _discover_under("Private", prefix) == []


def test_sharepoint_root_prefix_is_the_explicit_whole_library_access() -> None:
    """The root prefix ``/`` is how a configuration allows every folder."""
    assert _discover_under("Private", "/") == ["f1"]


@pytest.mark.parametrize("prefix", ["Other/Q3 Plans", "Q3 Planning", "Shared/Q3 Plans"])
def test_sharepoint_prefix_still_refuses_other_folders(prefix: str) -> None:
    """A prefix that names another folder or library still allows nothing here."""
    assert _discover_under("Q3 Plans", prefix) == []


def test_sharepoint_library_chosen_by_id_has_its_names_looked_up() -> None:
    """A prefix that starts with the library name works when the library is set by drive_id."""
    client = _client_with_one_file("Q3 Plans")
    client.add("drives/drive1", LIBRARY)

    found = _discover(
        client, drive_id="drive1", allowed_path_prefixes=["Shared Documents/Q3 Plans"]
    )

    assert found == ["f1"]


def test_sharepoint_single_folder_prefix_needs_no_library_lookup() -> None:
    """A one-folder prefix never names a library, so the library's names are not fetched."""
    client = _client_with_one_file("Q3 Plans")

    found = _discover(client, drive_id="drive1", allowed_path_prefixes=["Q3 Plans"])

    assert found == ["f1"]
    assert "drives/drive1" not in [endpoint for endpoint, _ in client.calls]
