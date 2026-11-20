"""Regression: allowed_path_prefixes match the folders a user sees in SharePoint."""

from __future__ import annotations

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


def _discover_under(folder_name: str, prefix: str) -> list[str]:
    """Discover one file in a top-level folder of a library whose URL name is Shared Documents."""
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add(
        "sites/site1/drive",
        {
            "id": "drive1",
            "name": "Documents",
            "driveType": "documentLibrary",
            "webUrl": f"{SITE_URL}/Shared%20Documents",
        },
    )
    child = file_item("f1", "Design review.docx", parent_id="d1")
    child["parentReference"]["path"] = f"/drive/root:/{quote(folder_name)}"
    client.add("drives/drive1/root/children", {"value": [folder_item("d1", folder_name)]})
    client.add("drives/drive1/items/d1/children", {"value": [child]})
    connector = SharePointConnector(config(allowed_path_prefixes=[prefix]), client=client)
    return [record.locator for record in connector.discover()]


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


@pytest.mark.parametrize(
    "prefix", ["Shared Documents/Q3 Plans", "Documents/Q3 Plans", "Shared Documents"]
)
def test_sharepoint_prefix_may_start_with_the_library_name(prefix: str) -> None:
    """A prefix written from the library down matches the same folder inside it."""
    assert _discover_under("Q3 Plans", prefix) == ["f1"]


@pytest.mark.parametrize("prefix", ["Other/Q3 Plans", "Q3 Planning", "Shared/Q3 Plans"])
def test_sharepoint_prefix_still_refuses_other_folders(prefix: str) -> None:
    """A prefix that names another folder or library still allows nothing here."""
    assert _discover_under("Q3 Plans", prefix) == []
