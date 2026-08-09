from harborrag_core.domain.element import DocumentElement

from ..chunking_helpers import make_document, make_profile, make_request, make_service


def test_canonical_strategy_preserves_pdf_page_provenance() -> None:
    document = make_document(
        [
            DocumentElement(
                "page-1",
                "paragraph",
                "PDF page",
                {"page": 1, "ocr_confidence": 0.93},
            )
        ],
        content_type="Application/PDF; version=1.7",
    )

    result = make_service(make_profile()).chunk(make_request(document))

    assert result.strategy == "canonical"
    assert result.chunks[0].citation_locator.page_start == 1
    assert result.chunks[0].citation_locator.page_end == 1
    assert result.chunks[0].metadata["ocr_confidence"] == 0.93


def test_canonical_strategy_keeps_normalized_json_paths() -> None:
    document = make_document(
        [
            DocumentElement(
                "json-root",
                "metadata",
                "Normalized JSON",
                {"json_path": "$['root']"},
            )
        ],
        content_type="application/json",
        raw={"json": {"ignored": "raw provider value"}},
    )

    result = make_service(make_profile(target=40, maximum=50)).chunk(make_request(document))

    assert result.strategy == "canonical"
    assert result.chunks[0].metadata["json_path"] == "$['root']"
    assert result.chunks[0].citation_locator.source_element_ids == ("json-root",)


def test_raw_format_payload_does_not_change_canonical_chunks() -> None:
    elements = [DocumentElement("body", "paragraph", "Canonical evidence")]
    first = make_document(elements, raw={"html": "<p>first raw value</p>"})
    second = make_document(elements, raw={"html": "<p>different raw value</p>"})
    service = make_service(make_profile())

    first_result = service.chunk(make_request(first))
    second_result = service.chunk(make_request(second))

    assert first_result.chunks == second_result.chunks
    assert first_result.manifest.fingerprint == second_result.manifest.fingerprint
