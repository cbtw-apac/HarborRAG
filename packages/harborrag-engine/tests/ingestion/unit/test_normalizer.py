from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_engine.ingestion.normalizer import DocumentNormalizer

from .chunking_helpers import make_profile, make_request, make_service


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


def test_normalizer_builds_canonical_table_and_chunk_reuses_its_identity() -> None:
    raw = RawDocument(
        id="document-1",
        source="file:///docs/matrix.md",
        content="| Store | Check |\n| --- | --- |\n| Qdrant | vectors |",
        content_type="text/markdown",
        metadata={
            "source_version": "source-v7",
            "title": "Verification matrix",
        },
    )
    parsed = ParsedDocument(
        content="Store Check Qdrant vectors",
        parser_name="markdown",
        elements=[
            DocumentElement(
                id="heading-1",
                type="heading",
                content="Verification",
                metadata={"level": 1},
            ),
            DocumentElement(
                id="table-1",
                type="table",
                content="Store\tCheck\nQdrant\tvectors",
                metadata={"header_rows": 1},
            ),
        ],
    )

    document = DocumentNormalizer().normalize(raw, parsed)
    table = document.table_artifacts[0]
    table_element = document.content[1]
    chunks = make_service(make_profile(target=80, maximum=100)).chunk(make_request(document)).chunks
    table_chunk = next(chunk for chunk in chunks if chunk.chunk_kind.value == "table")

    assert table.section_path == ("Verification",)
    assert table.column_names == ("Store", "Check")
    assert table.document_id == document.id
    assert table_element.metadata["table_id"] == table.table_id
    assert table_element.metadata["table_version_id"] == table.table_version_id
    assert table_chunk.table_locator is not None
    assert table_chunk.table_locator.table_id == table.table_id
    assert table_chunk.table_locator.table_version_id == table.table_version_id
