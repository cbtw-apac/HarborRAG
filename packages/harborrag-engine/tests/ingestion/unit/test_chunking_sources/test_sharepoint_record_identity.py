"""SharePoint document fields are carried onto every chunk at the record layer.

The canonical strategy is used for SharePoint. These tests pin that the record
factory adds the document's fields to each chunk, that they replace candidate
copies, and that other connectors are left untouched.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_engine.ingestion.chunking import ChunkingRequest

from ..chunking_helpers import make_profile, make_service

pytestmark = pytest.mark.unit

URL = "https://contoso.sharepoint.com/sites/hr/Policy.docx"
AUTHOR = "Alex Example"
CREATED = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
UPDATED = datetime(2026, 5, 1, 6, 30, tzinfo=UTC)
PROVENANCE = DocumentProvenance(
    source=URL,
    record_id="sharepoint://SITE/DRIVE/ITEM",
    url=URL,
    author=AUTHOR,
    created_at=CREATED,
    updated_at=UPDATED,
    extra={"site_id": "SITE", "drive_id": "DRIVE", "item_id": "ITEM"},
)
SHAREPOINT_KEYS = (
    "site_id",
    "drive_id",
    "item_id",
    "page_id",
    "web_url",
    "author",
    "source_created_at",
    "source_updated_at",
)
ELEMENTS = (
    DocumentElement("h1", "heading", "Leave", {"level": 1}),
    DocumentElement("p1", "paragraph", "Annual leave is 25 days. " * 6),
    DocumentElement("p2", "paragraph", "Carry-over needs manager approval. " * 6),
)


def _request(
    *,
    connector_type: str = "sharepoint",
    provenance: DocumentProvenance = PROVENANCE,
    elements: tuple[DocumentElement, ...] = ELEMENTS,
) -> ChunkingRequest:
    document = Document(
        id="doc-1",
        title="Policy.docx",
        content=list(elements),
        content_type="application/octet-stream",
        provenance=provenance,
    )
    return ChunkingRequest(
        tenant_id="tenant-1",
        document_version_id="document-version:1",
        connector_type=connector_type,
        document=document,
    )


def _chunks(request: ChunkingRequest, *, route: bool = False) -> tuple[ChunkRecord, ...]:
    service = make_service(
        make_profile(target=20, maximum=25),
        create_route_chunks=route,
    )
    return tuple(service.chunk(request).chunks)


def _with_metadata(**metadata: str) -> tuple[DocumentElement, ...]:
    return tuple(
        replace(element, metadata={**element.metadata, **metadata}) for element in ELEMENTS
    )


def test_sharepoint_chunks_including_routes_carry_the_document_fields() -> None:
    """Every chunk, route chunks included, carries the document's SharePoint fields."""
    chunks = _chunks(_request(), route=True)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.metadata["site_id"] == "SITE"
        assert chunk.metadata["drive_id"] == "DRIVE"
        assert chunk.metadata["item_id"] == "ITEM"
        assert chunk.metadata["web_url"] == URL
        assert chunk.metadata["author"] == AUTHOR
        assert chunk.metadata["source_created_at"] == "2026-04-01T09:00:00+00:00"
        assert chunk.metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"


@pytest.mark.parametrize(
    "updated_at",
    [
        datetime(2026, 5, 1, 8, 30, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 5, 1, 6, 30),
    ],
    ids=["offset", "naive"],
)
def test_sharepoint_update_time_with_offset_or_naive_is_stored_in_utc(
    updated_at: datetime,
) -> None:
    """An update time with an offset, or without a time zone, is stored in UTC."""
    chunks = _chunks(_request(provenance=replace(PROVENANCE, updated_at=updated_at)))

    assert {chunk.metadata["source_updated_at"] for chunk in chunks} == {
        "2026-05-01T06:30:00+00:00"
    }


def test_sharepoint_time_without_utc_equivalent_is_left_out() -> None:
    """A time with no UTC equivalent is dropped instead of failing chunking."""
    earliest = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))

    chunks = _chunks(_request(provenance=replace(PROVENANCE, created_at=earliest)))

    for chunk in chunks:
        assert "source_created_at" not in chunk.metadata
        assert chunk.metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"


def test_sharepoint_blank_identity_and_author_are_left_out() -> None:
    """Blank IDs and a blank author are dropped, and present values are kept."""
    provenance = replace(PROVENANCE, extra={"site_id": "  ", "drive_id": "DRIVE"}, author="   ")
    request = _request(provenance=provenance)

    for chunk in _chunks(request):
        assert "site_id" not in chunk.metadata
        assert "item_id" not in chunk.metadata
        assert "author" not in chunk.metadata
        assert chunk.metadata["drive_id"] == "DRIVE"


def test_sharepoint_candidate_identity_is_replaced_by_the_document() -> None:
    """A candidate's own site and item IDs give way to the document's."""
    elements = _with_metadata(site_id="OTHER-SITE", item_id="OTHER-ITEM")

    for chunk in _chunks(_request(elements=elements)):
        assert chunk.metadata["site_id"] == "SITE"
        assert chunk.metadata["item_id"] == "ITEM"


def test_sharepoint_site_page_drops_candidate_drive_and_item() -> None:
    """A site page's chunks never keep a drive or item ID from a candidate."""
    page = replace(PROVENANCE, extra={"site_id": "SITE", "page_id": "PAGE"})
    elements = _with_metadata(drive_id="OTHER-DRIVE", item_id="OTHER-ITEM")

    for chunk in _chunks(_request(provenance=page, elements=elements)):
        assert chunk.metadata["page_id"] == "PAGE"
        assert "drive_id" not in chunk.metadata
        assert "item_id" not in chunk.metadata


@pytest.mark.parametrize("connector_type", ["confluence", "jira"])
def test_other_connector_chunks_get_no_sharepoint_fields(connector_type: str) -> None:
    """Confluence and Jira chunks get none of the SharePoint fields."""
    for chunk in _chunks(_request(connector_type=connector_type)):
        for key in SHAREPOINT_KEYS:
            assert key not in chunk.metadata, key
