from datetime import UTC, datetime

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_engine.ingestion.chunking import ChunkingRequest

from ..chunking_helpers import make_profile, make_service

URL = "https://contoso.sharepoint.com/sites/hr/Policy.docx"


def _request(*, extra: dict[str, object]) -> ChunkingRequest:
    document = Document(
        id="doc-1",
        title="Policy.docx",
        content=[
            DocumentElement("h1", "heading", "Leave", {"level": 1}),
            DocumentElement("p1", "paragraph", "Annual leave is 25 days."),
            DocumentElement("p2", "paragraph", "Carry-over needs approval."),
        ],
        content_type="application/octet-stream",
        provenance=DocumentProvenance(
            source=URL,
            record_id="sharepoint://SITE/DRIVE/ITEM",
            url=URL,
            author="Ada Lovelace",
            updated_at=datetime(2026, 5, 1, 6, 30, tzinfo=UTC),
            extra=extra,
        ),
    )
    return ChunkingRequest(
        tenant_id="tenant-1",
        document_version_id="document-version:1",
        connector_type="sharepoint",
        document=document,
    )


def test_sharepoint_chunks_carry_source_identity_on_every_point() -> None:
    request = _request(extra={"site_id": "SITE", "drive_id": "DRIVE", "item_id": "ITEM"})
    profile = make_profile(name="sharepoint", strategy="sharepoint", target=20, maximum=25)

    result = make_service(profile).chunk(request)

    assert result.chunks
    for chunk in result.chunks:
        assert chunk.metadata["site_id"] == "SITE"
        assert chunk.metadata["drive_id"] == "DRIVE"
        assert chunk.metadata["item_id"] == "ITEM"
        assert chunk.metadata["web_url"] == URL
        assert chunk.metadata["author"] == "Ada Lovelace"
        # Falls back to the canonical updated_at when admission has not stamped it.
        assert chunk.metadata["source_updated_at"] == "2026-05-01T06:30:00+00:00"
