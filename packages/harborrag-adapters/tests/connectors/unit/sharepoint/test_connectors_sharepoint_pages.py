"""Unit tests for SharePoint modern site page discovery and loading."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sharepoint_test_helpers import (
    SITE_URL,
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    fixture,
    page_item,
)

from harborrag_adapters.connectors import SharePointConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.mappers import is_page_record
from harborrag_adapters.connectors.sharepoint.pages import render_page_html
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]

PAGES_ENDPOINT = "sites/site1/pages/microsoft.graph.sitePage"
PAGE1_ENDPOINT = "sites/site1/pages/page1/microsoft.graph.sitePage"


def _page_record(page_id: str = "page1", site_id: str = "site1") -> SourceRecord:
    return SourceRecord(
        f"sharepoint://{site_id}/pages/{page_id}",
        "text/html",
        page_id,
        metadata={"record_kind": "page", "site_id": site_id, "page_id": page_id},
    )


def test_include_pages_defaults_off():
    assert config().include_pages is False
    assert config(include_pages=True).include_pages is True


def test_discover_without_include_pages_never_calls_pages_api():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/root/children", {"value": [file_item("file1", "A.docx")]})
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover())

    assert [record.id for record in records] == ["sharepoint://site1/drive1/file1"]
    assert all(endpoint != PAGES_ENDPOINT for endpoint, _ in client.calls)


def test_discover_maps_pages_before_files():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(PAGES_ENDPOINT, fixture("pages_list.json"))
    client.add("drives/drive1/root/children", {"value": [file_item("file1", "A.docx")]})
    connector = SharePointConnector(config(include_pages=True), client=client)

    records = list(connector.discover())

    assert [record.id for record in records] == [
        "sharepoint://site1/pages/page1",
        "sharepoint://site1/pages/page2",
        "sharepoint://site1/drive1/file1",
    ]
    page = records[0]
    assert is_page_record(page)
    assert not is_page_record(records[2])
    assert page.source_type == "text/html"
    assert page.locator == "page1"
    assert page.checksum == '"{PAGE1-ETAG},2"'
    assert page.updated_at == datetime(2024, 5, 24, 20, 57, 56, tzinfo=UTC)
    assert page.metadata["record_kind"] == "page"
    assert page.metadata["site_id"] == "site1"
    assert page.metadata["page_id"] == "page1"
    assert page.metadata["title"] == "Onboarding"
    assert page.metadata["page_layout"] == "article"
    assert page.metadata["publishing_level"] == "published"
    assert page.metadata["created_by"] == "Ada"
    assert page.metadata["updated_by"] == "Grace"
    assert page.metadata.get("drive_id") is None


def test_discover_follows_pages_next_link():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        PAGES_ENDPOINT,
        {
            "value": [page_item("page1")],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/pages-2",
        },
    )
    client.add("https://graph.microsoft.com/v1.0/pages-2", {"value": [page_item("page2")]})
    client.add("drives/drive1/root/children", {"value": []})
    connector = SharePointConnector(config(include_pages=True), client=client)

    records = list(connector.discover())

    assert [record.locator for record in records] == ["page1", "page2"]
    assert ("https://graph.microsoft.com/v1.0/pages-2", None) in client.calls


def test_discover_rejects_pages_pagination_that_does_not_advance():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    loop = "https://graph.microsoft.com/v1.0/pages-loop"
    client.add(PAGES_ENDPOINT, {"value": [], "@odata.nextLink": loop})
    client.add(loop, {"value": [], "@odata.nextLink": loop})
    connector = SharePointConnector(config(include_pages=True), client=client)

    with pytest.raises(FetchError, match="pages pagination did not advance"):
        list(connector.discover())


def test_discover_limit_counts_pages_and_files_together():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(PAGES_ENDPOINT, {"value": [page_item("page1")]})
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), file_item("file2", "B.docx")]},
    )
    connector = SharePointConnector(config(include_pages=True), client=client)

    assert [r.locator for r in connector.discover(ConnectorQuery(limit=1))] == ["page1"]
    client.add(PAGES_ENDPOINT, {"value": [page_item("page1")]})
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), file_item("file2", "B.docx")]},
    )
    assert [r.locator for r in connector.discover(ConnectorQuery(limit=2))] == [
        "page1",
        "file1",
    ]


def test_discover_filters_pages_by_updated_after_and_pattern():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(PAGES_ENDPOINT, fixture("pages_list.json"))
    client.add("drives/drive1/root/children", {"value": []})
    connector = SharePointConnector(config(include_pages=True), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(updated_after=datetime(2024, 1, 1, tzinfo=UTC), pattern="*.aspx")
        )
    )

    assert [record.locator for record in records] == ["page1"]


def test_discover_drive_selectors_skip_pages():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(include_pages=True), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": ["file1"]})))

    assert [record.locator for record in records] == ["file1"]
    assert all(endpoint != PAGES_ENDPOINT for endpoint, _ in client.calls)


def test_drive_walk_never_ingests_page_files_as_files():
    def run(include_pages: bool) -> tuple[list[str], list[str]]:
        client = FakeGraphClient()
        add_site_and_default_drive(client)
        client.add(PAGES_ENDPOINT, {"value": [page_item("page1")]})
        client.add(
            "drives/drive1/root/children",
            {"value": [file_item("aspx1", "Onboarding.aspx"), file_item("file1", "A.docx")]},
        )
        connector = SharePointConnector(config(include_pages=include_pages), client=client)
        records = [record.id for record in connector.discover()]
        return records, [skip.reason for skip in connector.skipped]

    # Pages off: .aspx has no parser, so it is a reported unsupported file.
    assert run(False) == (["sharepoint://site1/drive1/file1"], ["UNSUPPORTED_EXTENSION"])
    # Pages on: the pages API owns the page, so the drive copy is not a skip.
    assert run(True) == (
        ["sharepoint://site1/pages/page1", "sharepoint://site1/drive1/file1"],
        [],
    )


def test_load_page_renders_text_web_parts_in_page_order():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(PAGE1_ENDPOINT, fixture("page_canvas.json"))
    connector = SharePointConnector(config(include_pages=True), client=client)

    document = connector.load(_page_record())

    assert document.id == "sharepoint://site1/pages/page1"
    assert document.content_type == "text/html"
    assert document.source == f"{SITE_URL}/SitePages/Onboarding.aspx"
    assert isinstance(document.content, str)
    assert document.content.splitlines() == [
        "<h1>Onboarding</h1>",
        "<h2>First week</h2><p>Request laptop access.</p>",
        "<p>Meet your buddy.</p>",
        "<h2>Second week</h2><p>Ship a first change.</p>",
        "<p>Contact: the platform team.</p>",
    ]
    assert "Team photo" not in document.content
    assert "Quick links" not in document.content
    assert (PAGE1_ENDPOINT, {"$expand": "canvasLayout"}) in client.calls
    assert client.byte_calls == []

    metadata = document.metadata
    assert metadata["source_system"] == "sharepoint"
    assert metadata["record_kind"] == "page"
    assert metadata["record_id"] == "page1"
    assert metadata["title"] == "Onboarding"
    assert metadata["page_id"] == "page1"
    assert metadata["item_name"] == "Onboarding.aspx"
    assert metadata["site_id"] == "site1"
    assert metadata["site_name"] == "Engineering"
    assert metadata["has_text_content"] is True
    assert metadata["checksum"]


def test_load_page_without_text_keeps_the_title():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        PAGE1_ENDPOINT,
        page_item(
            "page1",
            title="Gallery & photos",
            canvasLayout={
                "horizontalSections": [
                    {
                        "columns": [
                            {
                                "webparts": [
                                    {
                                        "@odata.type": "#microsoft.graph.standardWebPart",
                                        "data": {"title": "Image"},
                                    },
                                    {"@odata.type": "#microsoft.graph.textWebPart"},
                                    "not-a-web-part",
                                ]
                            }
                        ]
                    }
                ]
            },
        ),
    )
    connector = SharePointConnector(config(include_pages=True), client=client)

    document = connector.load(_page_record())

    assert document.content == "<h1>Gallery &amp; photos</h1>"
    assert document.metadata["has_text_content"] is False


def test_render_page_html_tolerates_missing_canvas():
    html, has_text = render_page_html({"id": "page1", "name": "Blank.aspx"})

    assert html == "<h1>Blank.aspx</h1>"
    assert has_text is False


def test_load_page_rejects_records_from_another_site():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    connector = SharePointConnector(config(include_pages=True), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured site"):
        connector.load(_page_record(site_id="other-site"))
    assert all(endpoint != PAGE1_ENDPOINT for endpoint, _ in client.calls)


def test_load_page_rejected_when_pages_are_off():
    client = FakeGraphClient()
    connector = SharePointConnector(config(), client=client)

    with pytest.raises(DocumentProcessingError, match="page loading is disabled"):
        connector.load(_page_record())
    assert client.calls == []
