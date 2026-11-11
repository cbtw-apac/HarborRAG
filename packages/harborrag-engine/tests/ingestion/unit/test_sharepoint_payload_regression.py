"""Regression: SharePoint source metadata survives ingestion into Qdrant payloads.

Runs the real path (connector metadata, normalizer, chunker, vector projection)
and compares every evidence point's payload with the source metadata.
"""

from __future__ import annotations

from harborrag_adapters.connectors.sharepoint.mappers import build_document_metadata
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_engine.ingestion import VectorProjectionBuilder, VectorProjectionInput
from harborrag_engine.ingestion.chunking import ChunkingRequest
from harborrag_engine.ingestion.normalizer import DocumentNormalizer

from .chunking_helpers import make_profile, make_service
from .test_vector_projection import chunk_set, representation_set

SITE = {"id": "contoso.sharepoint.com,SITE,WEB", "name": "hr"}
DRIVE = {"id": "DRIVE-1", "name": "Documents", "driveType": "documentLibrary"}
URL = "https://contoso.sharepoint.com/sites/hr/Shared%20Documents/Policy.docx"
ITEM = {
    "id": "ITEM-7",
    "name": "Policy.docx",
    "webUrl": URL,
    "size": 2048,
    "eTag": "etag-1",
    "cTag": "ctag-1",
    "createdDateTime": "2026-04-01T09:00:00Z",
    "lastModifiedDateTime": "2026-05-01T08:30:00+02:00",
    "createdBy": {"user": {"displayName": "Ada Lovelace"}},
    "lastModifiedBy": {"user": {"displayName": "Grace Hopper"}},
    "parentReference": {"driveId": "DRIVE-1", "id": "PARENT", "path": "/drives/DRIVE-1/root:"},
    "file": {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": "h"}},
}
BODY = (
    "Annual leave is 25 days for every employee. " * 4
    + "Carry-over of unused leave needs manager approval before March. " * 4
)


def test_every_sharepoint_point_payload_matches_source_metadata() -> None:
    source = build_document_metadata(ITEM, site=SITE, drive=DRIVE, checksum="sum").to_dict()
    raw = RawDocument(
        id="sharepoint://SITE/DRIVE-1/ITEM-7",
        source=URL,
        content=BODY,
        content_type="text/plain",
        metadata=source,
    )
    document = DocumentNormalizer().normalize(
        raw, ParsedDocument(content=BODY, parser_name="plain-text")
    )

    service = make_service(
        make_profile(name="sharepoint", strategy="sharepoint", minimum=20, target=60, maximum=80)
    )
    chunks = service.chunk(
        ChunkingRequest(
            tenant_id="tenant-1",
            document_version_id="document-version:1",
            connector_type="sharepoint",
            document=document,
        )
    ).chunks
    assert len(chunks) > 1, "fixture must produce several chunks to test consistency"

    batch = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=chunks,
            representations=representation_set(chunks),
            chunk_artifacts=chunk_set(chunks),
        )
    )
    payloads = [
        record.payload.model_dump(mode="json", exclude_none=True)
        for record in batch.evidence_records
    ]
    assert len(payloads) == len(chunks)

    expected = {
        "site_id": "contoso.sharepoint.com,SITE,WEB",
        "drive_id": "DRIVE-1",
        "item_id": "ITEM-7",
        "web_url": URL,
        "author": "Ada Lovelace",
        "updated_at": "2026-05-01T06:30:00+00:00",
    }
    for payload in payloads:
        for key, value in expected.items():
            assert payload.get(key) == value, f"{key} missing or wrong on a point"
