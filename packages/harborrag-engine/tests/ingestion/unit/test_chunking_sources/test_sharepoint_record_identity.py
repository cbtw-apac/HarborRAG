"""SharePoint identity is carried onto every chunk at the record layer.

The canonical strategy is used for SharePoint; these tests pin that the record
factory adds the identity to each chunk and leaves other connectors untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_engine.ingestion.chunking import ChunkingRequest

from ..chunking_helpers import make_profile, make_service

URL = "https://contoso.sharepoint.com/sites/hr/Policy.docx"
SHAREPOINT_KEYS = ("site_id", "drive_id", "item_id", "web_url", "author", "source_updated_at")
ELEMENTS = [
    DocumentElement("h1", "heading", "Leave", {"level": 1}),
    DocumentElement("p1", "paragraph", "Annual leave is 25 days. " * 6),
    DocumentElement("p2", "paragraph", "Carry-over needs manager approval. " * 6),
]


def _request(
    *,
    connector_type: str = "sharepoint",
    extra: dict[str, Any] | None = None,
    updated_at: datetime | None = datetime(2026, 5, 1, 6, 30, tzinfo=UTC),
) -> ChunkingRequest:
    document = Document(
        id="doc-1",
        title="Policy.docx",
        content=list(ELEMENTS),
        content_type="application/octet-stream",
        provenance=DocumentProvenance(
            source=URL,
            record_id="sharepoint://SITE/DRIVE/ITEM",
            url=URL,
            author="Ada Lovelace",
            updated_at=updated_at,
            extra=extra
            if extra is not None
            else {"site_id": "SITE", "drive_id": "DRIVE", "item_id": "ITEM"},
        ),
    )
    return ChunkingRequest(
        tenant_id="tenant-1",
        document_version_id="document-version:1",
        connector_type=connector_type,
        document=document,
    )


def _chunk(request: ChunkingRequest, *, route: bool = False) -> Any:
    service = make_service(
        make_profile(target=20, maximum=25),
        create_route_chunks=route,
    )
    return service.chunk(request).chunks


def test_every_sharepoint_chunk_carries_identity_including_route_chunks() -> None:
    chunks = _chunk(_request(), route=True)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.metadata["site_id"] == "SITE"
        assert chunk.metadata["drive_id"] == "DRIVE"
        assert chunk.metadata["item_id"] == "ITEM"
        assert chunk.metadata["web_url"] == URL
        assert chunk.metadata["author"] == "Ada Lovelace"
        assert chunk.metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"


def test_sharepoint_timestamp_is_normalized_to_utc() -> None:
    request = _request(
        extra={"site_id": "SITE", "source_updated_at": "2026-05-01T08:30:00+02:00"},
        updated_at=None,
    )

    for chunk in _chunk(request):
        assert chunk.metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"


def test_sharepoint_timestamp_falls_back_to_provenance_and_accepts_z_suffix() -> None:
    request = _request(extra={"source_updated_at": "2026-05-01T06:30:00Z"}, updated_at=None)
    assert _chunk(request)[0].metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"

    fallback = _request(extra={}, updated_at=datetime(2026, 5, 1, 6, 30, tzinfo=UTC))
    assert _chunk(fallback)[0].metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"


def test_sharepoint_missing_fields_are_omitted_not_blank() -> None:
    request = _request(extra={"site_id": "  "}, updated_at=None)
    metadata = _chunk(request)[0].metadata

    assert "site_id" not in metadata
    assert "drive_id" not in metadata
    assert "item_id" not in metadata


def test_confluence_chunks_do_not_receive_sharepoint_identity() -> None:
    request = _request(
        connector_type="confluence",
        extra={"site_id": "SITE", "drive_id": "DRIVE", "item_id": "ITEM"},
    )

    for chunk in _chunk(request):
        for key in SHAREPOINT_KEYS:
            assert key not in chunk.metadata, key


def test_jira_chunks_do_not_receive_sharepoint_identity() -> None:
    request = _request(connector_type="jira", extra={"site_id": "SITE", "item_id": "ITEM"})

    for chunk in _chunk(request):
        assert "site_id" not in chunk.metadata
        assert "item_id" not in chunk.metadata
