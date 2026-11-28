"""Regression: Confluence titles with plus signs or only blanks keep their pages."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus, urlsplit

import pytest
from confluence_test_helpers import CLOUD_BASE, DC_BASE

from harborrag_adapters.connectors.confluence import ConfluenceDeploymentType
from harborrag_adapters.connectors.confluence.mappers import display_url
from harborrag_adapters.connectors.confluence.normalization import (
    ConfluencePageInput,
    ConfluencePageNormalizer,
)

pytestmark = pytest.mark.unit

NBSP = "\u00a0"
NNBSP = "\u202f"
SOURCE_URL = f"{DC_BASE}/display/ENG/page"


def _page_payload(title: str, *, ancestors: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": "123",
        "title": title,
        "type": "page",
        "space": {"key": "ENG", "id": 98765},
        "version": {"number": 1},
        "ancestors": ancestors,
        "body": {"storage": {"value": "<p>Synthetic body.</p>"}},
    }


@pytest.mark.parametrize(
    "title",
    [
        "Q3 design review",
        "Q3  design review",
        f"Q3{NBSP}design{NNBSP}review",
        "C++ guide",
        "Pros + cons",
        "50% done",
        "a/b test",
    ],
)
def test_display_url_datacenter_title_round_trips_through_unquote_plus(title: str) -> None:
    """A Data Center URL path segment decodes back to the exact page title."""
    url = display_url(DC_BASE, ConfluenceDeploymentType.DATACENTER, "ENG", "1", title)

    path = urlsplit(url).path
    assert path.startswith("/display/ENG/")
    assert unquote_plus(path.removeprefix("/display/ENG/")) == title


def test_display_url_datacenter_plus_in_title_is_percent_encoded() -> None:
    """A plus sign in a title is encoded as %2B, not left as an encoded space."""
    url = display_url(DC_BASE, ConfluenceDeploymentType.DATACENTER, "ENG", "1", "C++ guide")

    assert url == f"{DC_BASE}/display/ENG/C%2B%2B+guide"


def test_display_url_cloud_ignores_title() -> None:
    """Cloud URLs use the content ID and never the title."""
    url = display_url(CLOUD_BASE, ConfluenceDeploymentType.CLOUD, "ENG", "1", "C++ guide")

    assert url == f"{CLOUD_BASE}/spaces/ENG/pages/1"


@pytest.mark.parametrize("title", [NBSP, f"{NBSP}{NNBSP} ", ""])
def test_normalize_payload_blank_page_title_falls_back_to_page_id(title: str) -> None:
    """A page whose title is blank normalizes with its ID as the title."""
    payload = _page_payload(title, ancestors=[{"id": "100", "title": "Parent page"}])

    document = ConfluencePageNormalizer().normalize_payload(payload, source_url=SOURCE_URL)

    assert document.title == "123"
    assert document.provenance.extra["ancestor_ids"] == ("100",)
    assert document.provenance.extra["ancestor_titles"] == ("Parent page",)


@pytest.mark.parametrize("blank_title", [NBSP, f"{NNBSP} "])
def test_normalize_payload_blank_ancestor_title_drops_only_that_ancestor(
    blank_title: str,
) -> None:
    """An ancestor with a blank title is dropped and the page still normalizes."""
    payload = _page_payload(
        "Child page",
        ancestors=[
            {"id": "100", "title": "Root page"},
            {"id": "200", "title": blank_title},
        ],
    )

    document = ConfluencePageNormalizer().normalize_payload(payload, source_url=SOURCE_URL)

    assert document.title == "Child page"
    assert document.provenance.extra["ancestor_ids"] == ("100",)
    assert document.provenance.extra["ancestor_titles"] == ("Root page",)


@pytest.mark.parametrize(
    "title",
    ["Q3 design review", "Resume ", f"Q3{NBSP}design{NNBSP}review", "C++ guide"],
)
def test_from_api_payload_non_blank_titles_are_kept_unchanged(title: str) -> None:
    """Titles that are not blank keep their exact whitespace."""
    payload = _page_payload(title, ancestors=[{"id": "100", "title": f"Parent{NBSP}page "}])

    page = ConfluencePageInput.from_api_payload(payload, source_url=SOURCE_URL)

    assert page.title == title
    assert page.ancestors == (("100", f"Parent{NBSP}page "),)
