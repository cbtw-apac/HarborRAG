from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_engine.ingestion.normalizer import DocumentNormalizer


def test_normalizer_preserves_parser_elements_and_merges_metadata() -> None:
    element = DocumentElement(id="heading-1", type="heading", content="Overview")
    raw = RawDocument(
        id="document-1",
        source="local",
        content="# Overview",
        content_type="text/markdown",
        metadata={"title": "Source title", "permissions": {"groups": ["readers"]}},
    )
    parsed = ParsedDocument(
        content="Overview",
        parser_name="markdown",
        elements=[element],
        metadata={"title": "Parsed title", "tags": ["guide"]},
        warnings=["minor warning"],
    )

    document = DocumentNormalizer().normalize(raw, parsed)

    assert document.title == "Parsed title"
    assert document.content == [element]
    assert document.provenance.permissions == {"groups": ["readers"]}
    assert document.provenance.tags == ["guide"]
    assert document.provenance.extra["parser_name"] == "markdown"
    assert document.provenance.extra["parser_warnings"] == ["minor warning"]


def test_normalizer_builds_one_fallback_element_for_unstructured_text() -> None:
    raw = RawDocument("document-1", "local", "hello", "text/plain")
    parsed = ParsedDocument(content="hello", parser_name="text")

    document = DocumentNormalizer().normalize(raw, parsed)

    assert len(document.content) == 1
    assert document.content[0].id == "document-1#content"
    assert document.content[0].content == "hello"
