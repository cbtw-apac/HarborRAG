from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import TableChunkLocator, TableProjectionType
from harborrag_core.domain import (
    CanonicalDocument,
    Document,
    DocumentBlock,
    DocumentBlockKind,
    DocumentProvenance,
    TableArtifact,
    TableGridSlot,
)


def test_canonical_document_is_the_existing_document_contract_with_additive_structure():
    paragraph = DocumentBlock(
        block_id="block:p",
        kind=DocumentBlockKind.PARAGRAPH,
        ordinal=0,
        parent_block_id="block:root",
        text="visible",
    )
    root = DocumentBlock(
        block_id="block:root",
        kind=DocumentBlockKind.DOCUMENT,
        ordinal=0,
        children=(paragraph,),
    )

    document = CanonicalDocument(
        id="confluence://ENG/42",
        title="Guide",
        content=[],
        content_type="confluence_page",
        provenance=DocumentProvenance(source="confluence"),
        blocks=(root,),
        body_representation="adf",
        warnings=("recoverable",),
    )

    assert CanonicalDocument is Document
    assert document.blocks[0].ordered_child_block_ids == ("block:p",)
    assert document.raw is None


def test_extended_table_locator_validates_columns_fragments_and_projection():
    locator = TableChunkLocator(
        table_id="table:1",
        table_version_id="table-version:1",
        row_start=4,
        row_end=4,
        column_count=3,
        key_column_indices=(0,),
        selected_column_indices=(0, 2),
        selected_columns=("Service", "CPU"),
        repeated_header_row_count=2,
        projection_type=TableProjectionType.COLUMNS,
        tab_path=("Production",),
        fragment_index=0,
        fragment_count=2,
    )

    assert locator.projection_type == TableProjectionType.COLUMNS
    assert locator.repeated_header_row_count == 2
    with pytest.raises(ValidationError, match="must match"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=0,
            row_end=1,
            column_count=2,
            selected_column_indices=(0,),
            selected_columns=("A", "B"),
        )
    with pytest.raises(ValidationError, match="provided together"):
        TableChunkLocator(
            table_id="table:1",
            table_version_id="table-version:1",
            row_start=0,
            row_end=1,
            column_count=2,
            fragment_index=0,
        )
    with pytest.raises(ValidationError, match="non-empty"):
        TableChunkLocator(**(locator.model_dump() | {"selected_columns": ("Service", " ")}))
    with pytest.raises(ValidationError, match="tab_path"):
        TableChunkLocator(**(locator.model_dump() | {"tab_path": (" ",)}))
    with pytest.raises(ValidationError, match="below fragment_count"):
        TableChunkLocator(**(locator.model_dump() | {"fragment_index": 2, "fragment_count": 2}))
    with pytest.raises(ValidationError, match="within column_count"):
        TableChunkLocator(**(locator.model_dump() | {"selected_column_indices": (0, 7)}))


def test_invalid_table_grid_reference_and_child_parent_are_rejected():
    with pytest.raises(ValidationError, match="unknown source cell"):
        TableArtifact(
            table_id="table:1",
            table_version_id="table-version:1",
            document_id="document:1",
            document_version_id="document-version:1",
            source_version="1",
            source_block_id="source-table",
            ordinal=0,
            row_count=1,
            column_count=1,
            column_names=("Column 1",),
            header_hierarchy=((),),
            cells=(),
            logical_grid=((TableGridSlot(cell_id="missing"),),),
            content_hash="sha256",
        )

    child = DocumentBlock(
        block_id="child",
        kind=DocumentBlockKind.PARAGRAPH,
        ordinal=0,
        parent_block_id="wrong-parent",
        text="content",
    )
    with pytest.raises(ValidationError, match="reference their parent"):
        DocumentBlock(
            block_id="root",
            kind=DocumentBlockKind.DOCUMENT,
            ordinal=0,
            children=(child,),
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {
                "kind": DocumentBlockKind.HEADING,
                "heading_level": None,
            },
            "require heading_level",
        ),
        (
            {
                "kind": DocumentBlockKind.PARAGRAPH,
                "heading_level": 2,
            },
            "only valid",
        ),
        (
            {
                "kind": DocumentBlockKind.PARAGRAPH,
                "section_path": ("",),
            },
            "non-empty",
        ),
        (
            {
                "kind": DocumentBlockKind.PARAGRAPH,
                "tab_path": ("Production", " "),
            },
            "non-empty",
        ),
        (
            {
                "kind": DocumentBlockKind.PARAGRAPH,
                "container_path": ("Panel", ""),
            },
            "non-empty",
        ),
    ],
)
def test_document_block_structural_invariants(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DocumentBlock(block_id="block", ordinal=0, **values)


def test_document_block_rejects_blank_identifiers_and_duplicate_children() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        DocumentBlock(
            block_id="block",
            kind=DocumentBlockKind.PARAGRAPH,
            ordinal=0,
            section_id=" ",
        )

    child = DocumentBlock(
        block_id="child",
        kind=DocumentBlockKind.PARAGRAPH,
        ordinal=0,
        parent_block_id="root",
    )
    with pytest.raises(ValidationError, match="unique"):
        DocumentBlock(
            block_id="root",
            kind=DocumentBlockKind.DOCUMENT,
            ordinal=0,
            children=(child, child),
        )


def test_document_block_rejects_duplicate_and_out_of_order_ordinals() -> None:
    first = DocumentBlock(
        block_id="a", kind=DocumentBlockKind.PARAGRAPH, ordinal=0, parent_block_id="root"
    )
    duplicate_ordinal = DocumentBlock(
        block_id="b", kind=DocumentBlockKind.PARAGRAPH, ordinal=0, parent_block_id="root"
    )
    with pytest.raises(ValidationError, match="ordinals"):
        DocumentBlock(
            block_id="root",
            kind=DocumentBlockKind.DOCUMENT,
            ordinal=0,
            children=(first, duplicate_ordinal),
        )

    out_of_order_first = DocumentBlock(
        block_id="a", kind=DocumentBlockKind.PARAGRAPH, ordinal=1, parent_block_id="root"
    )
    out_of_order_second = DocumentBlock(
        block_id="b", kind=DocumentBlockKind.PARAGRAPH, ordinal=0, parent_block_id="root"
    )
    with pytest.raises(ValidationError, match="ordinals"):
        DocumentBlock(
            block_id="root",
            kind=DocumentBlockKind.DOCUMENT,
            ordinal=0,
            children=(out_of_order_first, out_of_order_second),
        )
