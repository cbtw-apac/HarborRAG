"""Unit tests for SharePoint connector URL/path/endpoint utility helpers."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors.sharepoint.utils import (
    children_endpoint,
    item_extension,
    item_mime_type,
    item_path,
    matches_pattern,
    parse_sharepoint_site_url,
    site_path_endpoint,
)
from sharepoint_test_helpers import folder_item

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_children_endpoint_supports_root_path_and_item_children():
    assert children_endpoint("drive1") == "drives/drive1/root/children"
    assert (
        children_endpoint("drive1", path="Shared Documents")
        == "drives/drive1/root:/Shared%20Documents:/children"
    )
    assert (
        children_endpoint("drive1", item_id="folder1")
        == "drives/drive1/items/folder1/children"
    )


def test_parse_sharepoint_site_url_rejects_non_absolute_url():
    with pytest.raises(ValueError, match="absolute SharePoint URL"):
        parse_sharepoint_site_url("not-a-url")


def test_site_path_endpoint_without_path():
    assert site_path_endpoint("contoso.sharepoint.com", None) == (
        "sites/contoso.sharepoint.com:/"
    )
    assert site_path_endpoint("contoso.sharepoint.com", "") == (
        "sites/contoso.sharepoint.com:/"
    )


def test_item_extension_returns_empty_without_dot():
    assert item_extension({"name": "README"}) == ""


def test_item_mime_type_folder_and_default_fallbacks():
    assert item_mime_type(folder_item()) == "application/vnd.microsoft.graph.folder"
    assert item_mime_type({"id": "x"}) == "application/octet-stream"


def test_item_path_without_root_marker_returns_name():
    item = {"name": "Doc.txt", "parentReference": {"path": "/drive/other"}}
    assert item_path(item) == "Doc.txt"


def test_item_path_with_empty_folder_path_returns_name():
    item = {"name": "Doc.txt", "parentReference": {"path": "/drive/root:"}}
    assert item_path(item) == "Doc.txt"


def test_matches_pattern_plain_substring():
    assert matches_pattern({"name": "Guide.docx"}, "guide") is True
    assert matches_pattern({"name": "Guide.docx"}, "missing") is False
