"""Regression: SharePoint source metadata survives ingestion into vector payloads.

Runs the real path, from connector metadata through the normalizer, the version
planner and the chunker to the vector projection, and compares every evidence
point's payload with the source metadata. It spans the SharePoint adapter and
the engine, so it lives in runtime, which depends on both.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_adapters.connectors.sharepoint.mappers import (
    build_document_metadata,
    build_page_document_metadata,
)
from harborrag_core.chunking import ChunkRecord, ConnectorType
from harborrag_core.domain.document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ArtifactReference,
    ChunkIndexEntry,
    ChunkRepresentation,
    ChunkSetArtifacts,
    DocumentVersionSnapshot,
    DocumentVersionState,
    ProcessingProfile,
    RepresentationSet,
    SourceAdmissionDecision,
    SourceBinding,
    SourceIdentity,
    SparseEncoderProfile,
    canonical_document_bytes,
    load_canonical_document,
)
from harborrag_core.testing.chunking import CharacterCounter, CharacterRefiner
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    VectorProjectionBuilder,
    VectorProjectionInput,
)
from harborrag_engine.ingestion.admission import (
    CanonicalVersionPlanner,
    PlannedDocumentVersion,
    SourceAdmissionPolicy,
    source_version_from_document,
)
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingLimits,
    ChunkingProfile,
    ChunkingRequest,
    build_chunking_service,
)
from harborrag_engine.ingestion.chunking.records.sharepoint import sharepoint_chunk_fields
from harborrag_engine.ingestion.normalizer import DocumentNormalizer

pytestmark = pytest.mark.unit

SITE = {"id": "contoso.sharepoint.com,SITE,WEB", "name": "hr"}
DRIVE = {"id": "DRIVE-1", "name": "Documents", "driveType": "documentLibrary"}
URL = "https://contoso.sharepoint.com/sites/hr/Shared%20Documents/Policy.docx"
AUTHOR = "Alex Example"
ITEM = {
    "id": "ITEM-7",
    "name": "Policy.docx",
    "webUrl": URL,
    "size": 2048,
    "eTag": "etag-1",
    "cTag": "ctag-1",
    "createdDateTime": "2026-04-01T09:00:00Z",
    "lastModifiedDateTime": "2026-05-01T08:30:00+02:00",
    "createdBy": {"user": {"displayName": AUTHOR}},
    "lastModifiedBy": {"user": {"displayName": "Sam Sample"}},
    "parentReference": {"driveId": "DRIVE-1", "id": "PARENT", "path": "/drives/DRIVE-1/root:"},
    "file": {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": "h"}},
}
ITEM_RAW_ID = "sharepoint://SITE/DRIVE-1/ITEM-7"
PAGE_URL = "https://contoso.sharepoint.com/sites/hr/SitePages/Welcome.aspx"
PAGE = {
    "id": "PAGE-3",
    "name": "Welcome.aspx",
    "title": "Welcome",
    "webUrl": PAGE_URL,
    "createdDateTime": "2026-04-02T09:00:00Z",
    "lastModifiedDateTime": "2026-05-03T10:15:00Z",
    "eTag": "etag-page",
    "createdBy": {"user": {"displayName": AUTHOR}},
    "lastModifiedBy": {"user": {"displayName": "Sam Sample"}},
}
BODY = (
    "Annual leave is 25 days for every employee. " * 4
    + "Carry-over of unused leave needs manager approval before March. " * 4
)
FILE_SOURCE = SourceIdentity(
    tenant_id="tenant-1",
    connector_type=ConnectorType.SHAREPOINT,
    connection_id="sharepoint-prod",
    source_item_id="ITEM-7",
    source_scope_id="SITE/DRIVE-1",
    binding=SourceBinding(kind="ROOT"),
)
PAGE_SOURCE = FILE_SOURCE.model_copy(
    update={"source_item_id": "PAGE-3", "source_scope_id": "SITE/pages"}
)
PROCESSING = ProcessingProfile(
    parser_profile="plain-text-v1",
    normalizer_version="canonical-v1",
    chunk_strategy="canonical",
    dense_encoder_profile="dense-v1",
    sparse_encoder_profile="bm25-v1",
    graph_projection_version="graph-v1",
)


def _file_document(item: dict[str, object] = ITEM) -> Document:
    metadata = build_document_metadata(item, site=SITE, drive=DRIVE, checksum="sum").to_dict()
    return _normalize(metadata, raw_id=ITEM_RAW_ID, url=URL)


def _normalize(metadata: dict[str, object], *, raw_id: str, url: str) -> Document:
    raw = RawDocument(
        id=raw_id,
        source=url,
        content=BODY,
        content_type="text/plain",
        metadata=metadata,
    )
    return DocumentNormalizer().normalize(
        raw, ParsedDocument(content=BODY, parser_name="plain-text")
    )


def _plan(document: Document, source: SourceIdentity = FILE_SOURCE) -> PlannedDocumentVersion:
    return CanonicalVersionPlanner().plan(
        document=document,
        source_identity=source,
        admission=AdmissionSnapshot(source_version=source_version_from_document(document)),
        processing=PROCESSING,
    )


def _stored(document: Document) -> Document:
    """Return the document as the chunk stage loads it from the canonical artifact store."""
    return load_canonical_document(canonical_document_bytes(document))


def _chunks(planned: PlannedDocumentVersion) -> tuple[ChunkRecord, ...]:
    profile = ChunkingProfile(
        name="canonical",
        strategy="canonical",
        limits=ChunkingLimits(20, 60, 80, 0),
    )
    service = build_chunking_service(
        config=ChunkingConfig(
            configuration_version="1",
            default_profile=profile.name,
            create_route_chunks=False,
            profiles={profile.name: profile},
            source_profiles={},
        ),
        token_counter=CharacterCounter(),
        refiner=CharacterRefiner(),
    )
    chunks = service.chunk(
        ChunkingRequest(
            tenant_id="tenant-1",
            document_version_id=str(planned.candidate.document_version_id),
            connector_type=ConnectorType.SHAREPOINT.value,
            document=_stored(planned.document),
        )
    ).chunks
    assert len(chunks) > 1, "fixture must produce several chunks to test consistency"
    return tuple(chunks)


def _payloads(planned: PlannedDocumentVersion) -> list[dict[str, object]]:
    chunks = _chunks(planned)
    batch = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=chunks,
            representations=_representation_set(chunks),
            chunk_artifacts=_chunk_set(chunks),
        )
    )
    assert len(batch.evidence_records) == len(chunks)
    return [
        record.payload.model_dump(mode="json", exclude_none=True)
        for record in batch.evidence_records
    ]


def test_sharepoint_file_points_carry_the_source_metadata() -> None:
    """Every point of a drive file carries its site, drive, item, link, author and times."""
    expected = {
        "site_id": "contoso.sharepoint.com,SITE,WEB",
        "drive_id": "DRIVE-1",
        "item_id": "ITEM-7",
        "web_url": URL,
        "author": AUTHOR,
        "created_at": "2026-04-01T09:00:00+00:00",
        "modified": "2026-05-01T06:30:00+00:00",
    }

    for payload in _payloads(_plan(_file_document())):
        for key, value in expected.items():
            assert payload.get(key) == value, f"{key} missing or wrong on a point"


def test_sharepoint_site_page_points_carry_page_identity_without_drive_identity() -> None:
    """A site page's points carry its page ID and no drive or item ID."""
    metadata = build_page_document_metadata(
        PAGE, site=SITE, title="Welcome", checksum="sum", has_text_content=True
    ).to_dict()
    document = _normalize(metadata, raw_id="sharepoint://SITE/pages/PAGE-3", url=PAGE_URL)

    payloads = _payloads(_plan(document, PAGE_SOURCE))

    assert payloads
    for payload in payloads:
        assert payload.get("page_id") == "PAGE-3"
        assert payload.get("site_id") == "contoso.sharepoint.com,SITE,WEB"
        assert payload.get("web_url") == PAGE_URL
        assert "drive_id" not in payload
        assert "item_id" not in payload


def test_sharepoint_update_time_only_change_is_a_new_version_with_the_new_time() -> None:
    """A changed update time alone is METADATA_CHANGED and reaches the new version's points."""
    first = _plan(_file_document())
    active = DocumentVersionSnapshot(
        document_id=first.candidate.document_id,
        document_version_id=first.candidate.document_version_id,
        fingerprints=first.candidate.fingerprints,
        state=DocumentVersionState.ACTIVE,
    )
    policy = SourceAdmissionPolicy()

    later = _plan(_file_document({**ITEM, "lastModifiedDateTime": "2026-06-01T10:00:00Z"}))
    repeat = _plan(_file_document())

    assert (
        policy.after_normalization(active=active, fingerprints=later.candidate.fingerprints)
        == SourceAdmissionDecision.METADATA_CHANGED
    )
    for payload in _payloads(later):
        assert payload["document_version_id"] == str(later.candidate.document_version_id)
        assert payload.get("modified") == "2026-06-01T10:00:00+00:00"
    assert (
        policy.after_normalization(active=active, fingerprints=repeat.candidate.fingerprints)
        == SourceAdmissionDecision.UNCHANGED
    )


def test_sharepoint_fingerprinted_fields_match_the_stored_document() -> None:
    """Admission hashes the same SharePoint fields the chunk stage reads from storage."""
    document = _file_document()

    stored = _stored(_plan(document).document)

    assert sharepoint_chunk_fields(document) == sharepoint_chunk_fields(stored)


def test_sharepoint_blank_author_is_left_out_instead_of_failing_the_batch() -> None:
    """A blank author is dropped from every point rather than failing the projection."""
    document = _file_document()
    blank = replace(document, provenance=replace(document.provenance, author="   "))

    for payload in _payloads(_plan(blank)):
        assert "author" not in payload


def _chunk_set(chunks: tuple[ChunkRecord, ...]) -> ChunkSetArtifacts:
    entries = tuple(
        ChunkIndexEntry(
            chunk_id=str(chunk.chunk_id),
            byte_offset=index * 1000,
            byte_length=900,
        )
        for index, chunk in enumerate(chunks)
    )
    return ChunkSetArtifacts(
        chunks=ArtifactReference(
            bucket="harborrag-artifacts",
            key="chunks/document-1/version-1.jsonl",
            sha256="a" * 64,
            byte_size=len(entries) * 1000,
            media_type="application/x-ndjson",
        ),
        index=ArtifactReference(
            bucket="harborrag-artifacts",
            key="chunks/document-1/version-1.idx",
            sha256="b" * 64,
            byte_size=200,
            media_type="application/x-ndjson",
        ),
        entries=entries,
    )


def _representation_set(chunks: tuple[ChunkRecord, ...]) -> RepresentationSet:
    sparse_encoder = BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1"))
    return RepresentationSet(
        document_id=chunks[0].document_id,
        document_version_id=chunks[0].document_version_id,
        dense_profile_id="dense-v1",
        sparse_profile_id=sparse_encoder.profile.profile_id,
        dense_dimension=3,
        records=tuple(
            ChunkRepresentation(
                chunk_id=str(chunk.chunk_id),
                dense_vector=[float(index), 1.0, 0.5],
                sparse_vector=sparse_encoder.encode(chunk.search_text).vector,
            )
            for index, chunk in enumerate(chunks, start=1)
        ),
    )
