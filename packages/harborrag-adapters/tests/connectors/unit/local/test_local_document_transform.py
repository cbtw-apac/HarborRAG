"""Local connector document transformation tests."""

from harborrag_adapters.connectors.local.document_transform import LocalDocumentTransform
from harborrag_core.domain import (
    Document,
    DocumentElement,
    DocumentProvenance,
    ParsedDocument,
    RawDocument,
)


def test_local_transform_uses_first_level_one_heading_as_title() -> None:
    raw = RawDocument(
        id="file:///docs/runbook.md",
        source="file:///docs/runbook.md",
        content="# Heading title",
        content_type="text/markdown",
    )
    parsed = ParsedDocument(content="Heading title", parser_name="markdown")
    document = Document(
        id=raw.id,
        title="runbook.md",
        content=[
            DocumentElement(
                id="heading",
                type="heading",
                content="Heading title",
                metadata={"level": 1},
            )
        ],
        content_type=raw.content_type,
        provenance=DocumentProvenance(source=raw.source),
    )

    transformed = LocalDocumentTransform().transform(raw, parsed, document)

    assert transformed.title == "Heading title"
