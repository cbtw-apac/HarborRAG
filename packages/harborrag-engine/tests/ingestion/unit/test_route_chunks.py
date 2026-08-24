from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind, RecordKind
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import UnsupportedDocumentError
from harborrag_engine.ingestion import produces_evidence

from .chunking_helpers import make_document, make_profile, make_request, make_service


def test_release_chunking_starts_with_required_document_route() -> None:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Worker Configuration", {"level": 1}),
            DocumentElement(
                "p1",
                "paragraph",
                "The activity timeout is 30 seconds.",
            ),
        ],
        source="confluence",
        record_id="95518771",
        extra={
            "page_id": "95518771",
            "space_key": "HARBORRAG",
            "labels": ["operations", "temporal"],
        },
    )

    result = make_service(
        make_profile(
            name="confluence",
            strategy="confluence",
            target=100,
            maximum=120,
        ),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(document))

    route, section_route, evidence = result.chunks
    assert route.record_kind == RecordKind.ROUTE
    assert route.chunk_kind == ChunkKind.TEXT
    assert route.ordinal == 0
    assert "Title: HarborRAG" in route.content
    assert "Source ID: 95518771" in route.search_text
    assert "Space Key: HARBORRAG" in route.search_text
    assert "Labels: operations, temporal" in route.search_text
    assert section_route.record_kind == RecordKind.ROUTE
    assert section_route.metadata["route_level"] == "section"
    assert evidence.record_kind == RecordKind.EVIDENCE
    assert evidence.chunk_kind == ChunkKind.TEXT
    assert "95518771" not in evidence.search_text
    assert "doc-1" not in evidence.search_text
    assert evidence.content in evidence.search_text
    assert result.statistics.route_chunk_count == 2
    assert result.statistics.evidence_chunk_count == 1


def test_route_identity_is_deterministic_for_identical_version_input() -> None:
    document = make_document(
        [DocumentElement("p1", "paragraph", "Stable evidence")],
        source="local_file",
        record_id="guide.md",
        extra={"relative_path": "docs/guide.md"},
    )
    service = make_service(
        make_profile(target=100, maximum=120),
        configuration_version="3",
        create_route_chunks=True,
    )

    first = service.chunk(make_request(document))
    second = service.chunk(make_request(document))

    assert first.chunks[0] == second.chunks[0]
    assert first.manifest.fingerprint == second.manifest.fingerprint


def test_each_section_starts_with_a_typed_route_chunk() -> None:
    document = make_document(
        [
            DocumentElement("title", "heading", "Release Guide", {"level": 1}),
            DocumentElement("section", "heading", "Rollback", {"level": 2}),
            DocumentElement("evidence", "paragraph", "Restore the previous image."),
        ]
    )

    result = make_service(
        make_profile(target=100, maximum=120),
        create_route_chunks=True,
    ).chunk(make_request(document))

    document_route, section_route, evidence = result.chunks
    assert document_route.record_kind == RecordKind.ROUTE
    assert document_route.metadata["route_level"] == "document"
    assert section_route.record_kind == RecordKind.ROUTE
    assert section_route.chunk_kind == ChunkKind.TEXT
    assert section_route.hierarchy.section_path == ("Release Guide", "Rollback")
    assert section_route.metadata["route_level"] == "section"
    assert evidence.record_kind == RecordKind.EVIDENCE
    assert evidence.hierarchy.section_path == ("Release Guide", "Rollback")


def test_heading_only_document_produces_a_provenance_backed_route() -> None:
    """The chunker keeps this defense, but the release path no longer reaches it.

    A route is not evidence, so the vector projection rejects the batch this shape
    produces. Admission therefore refuses a document with no evidence, and the runtime
    gives a titled ROOT page a title paragraph so it has some -- see
    ``produces_evidence`` below and ``runtime.ingestion.document.title_content``.
    """

    document = make_document(
        [DocumentElement("title", "heading", "Quality Reports", {"level": 1})],
        source="confluence",
        record_id="95518771",
        extra={"page_id": "95518771", "space_key": "HARBORRAG"},
    )

    result = make_service(
        make_profile(name="confluence", strategy="confluence"),
        create_route_chunks=True,
    ).chunk(make_request(document))

    assert len(result.chunks) == 1
    route = result.chunks[0]
    assert route.record_kind == RecordKind.ROUTE
    assert route.citation_locator.source_element_ids == ("title",)
    assert route.metadata["page_id"] == "95518771"
    assert result.statistics.evidence_chunk_count == 0


def test_truly_empty_document_is_classified_as_unsupported() -> None:
    document = make_document([], source="confluence", extra={"page_id": "95518771"})

    with pytest.raises(UnsupportedDocumentError, match="no indexable source content"):
        make_service(
            make_profile(name="confluence", strategy="confluence"),
            create_route_chunks=True,
        ).chunk(make_request(document))


def test_produces_evidence_agrees_with_what_segmentation_emits() -> None:
    """The predicate admission gates on must match the segmenter, element for element.

    A route is not evidence: ``VectorProjectionBuilder`` keeps only ``RecordKind
    .EVIDENCE``, so the heading-only document above reaches the vector projection with
    an empty batch and the release fails at BuildProjections. Callers ask this before
    committing, so it has to answer for the same element types the segmenter skips.
    """

    heading_only = make_document(
        [DocumentElement("title", "heading", "Quality Reports", {"level": 1})],
        source="confluence",
        extra={"page_id": "95518771"},
    )
    with_prose = make_document(
        [
            DocumentElement("title", "heading", "Quality Reports", {"level": 1}),
            DocumentElement("p1", "paragraph", "Reports ship weekly."),
        ],
        source="confluence",
        extra={"page_id": "95518771"},
    )

    assert produces_evidence(heading_only) is False
    assert produces_evidence(with_prose) is True

    service = make_service(
        make_profile(name="confluence", strategy="confluence"),
        create_route_chunks=True,
    )
    for document, expected in ((heading_only, 0), (with_prose, 1)):
        statistics = service.chunk(make_request(document)).statistics
        assert statistics.evidence_chunk_count == expected
