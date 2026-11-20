"""Regression: SharePoint item names that end in whitespace keep their extension."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.sharepoint.drive_paths import item_extension

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    [
        "Design review.docx\u00a0",
        "Design review.docx\u202f",
        "Design review.docx\u200b",
        "Design review.docx",
    ],
)
def test_sharepoint_item_name_with_whitespace_keeps_its_extension(name: str) -> None:
    """A no-break space after the extension does not decline the file as unsupported."""
    assert item_extension({"name": name}) == ".docx"
