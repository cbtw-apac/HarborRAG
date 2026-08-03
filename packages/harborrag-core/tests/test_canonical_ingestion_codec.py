from __future__ import annotations

import pytest

from harborrag_core.domain import (
    Document,
    DocumentBlock,
    DocumentBlockKind,
    DocumentElement,
    DocumentProvenance,
    TableArtifact,
    TableCell,
    TableGridSlot,
)
from harborrag_core.ingestion import (
    canonical_document_bytes,
    load_canonical_document,
)


def canonical_document() -> Document:
    paragraph = DocumentBlock(
        block_id="block-paragraph",
        kind=DocumentBlockKind.PARAGRAPH,
        ordinal=0,
        parent_block_id="block-root",
        text="The timeout is 30 seconds.",
    )
    root = DocumentBlock(
        block_id="block-root",
        kind=DocumentBlockKind.DOCUMENT,
        ordinal=0,
        children=(paragraph,),
    )
    cell = TableCell(
        cell_id="cell-1",
        row_index=0,
        column_index=0,
        text="30",
        value=30,
    )
    table = TableArtifact(
        table_id="table-1",
        table_version_id="table-version-1",
        document_id="document-1",
        document_version_id="version-1",
        source_version="7",
        source_block_id="source-table",
        ordinal=0,
        row_count=1,
        column_count=1,
        column_names=("Timeout",),
        header_hierarchy=(("Timeout",),),
        cells=(cell,),
        logical_grid=((TableGridSlot(cell_id="cell-1"),),),
        content_hash="table-content-hash",
    )
    return Document(
        id="document-1",
        title="Release Guide",
        content=[
            DocumentElement(
                id="paragraph-1",
                type="paragraph",
                content="The timeout is 30 seconds.",
            )
        ],
        content_type="confluence_page",
        provenance=DocumentProvenance(
            source="confluence",
            record_id="page-1",
            tags=["release"],
        ),
        blocks=(root,),
        table_artifacts=(table,),
        body_representation="adf",
        warnings=("fallback-used",),
    )


def test_canonical_codec_round_trips_blocks_tables_and_provenance() -> None:
    document = canonical_document()

    restored = load_canonical_document(canonical_document_bytes(document))

    assert restored == document
    assert restored.blocks[0].children[0].text == "The timeout is 30 seconds."
    assert restored.table_artifacts[0].source_cell(0, 0).value == 30


def test_canonical_codec_rejects_runtime_metadata_recursively() -> None:
    document = canonical_document()
    document.provenance.extra["nested"] = {"trace_id": "runtime-only"}

    with pytest.raises(ValueError, match="runtime field"):
        canonical_document_bytes(document)
