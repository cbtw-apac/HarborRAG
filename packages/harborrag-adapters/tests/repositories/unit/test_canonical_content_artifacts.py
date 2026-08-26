from __future__ import annotations

import json

import pytest

from harborrag_adapters.repositories.object_store import (
    CanonicalCommentArtifactRepository,
    CanonicalTableArtifactRepository,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_core.domain import (
    Document,
    DocumentElement,
    DocumentProvenance,
    TableArtifact,
    TableCell,
    TableCellType,
    TableGridSlot,
)
from harborrag_core.schemas.storage import StorageOperationContext


def _context() -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id="tenant-a")


def _document() -> Document:
    return Document(
        id="document-1",
        title="Release guide",
        content=[
            DocumentElement(
                id="comment-element-2",
                type="paragraph",
                content="The rollout finished successfully.",
                metadata={
                    "role": "confluence.comment",
                    "comment_id": "comment-2",
                    "comment_kind": "REPLY",
                    "parent_comment_id": "comment-1",
                },
            )
        ],
        content_type="page",
        provenance=DocumentProvenance(
            source="https://wiki.example/pages/1",
            extra={
                "source_system": "confluence",
                "comments": [
                    {
                        "id": "comment-2",
                        "body": "<p>unrendered source body</p>",
                        "author": "Ada",
                        "updated_at": "2026-07-30T10:00:00Z",
                    },
                    {
                        "id": "comment-1",
                        "body": "Done",
                        "author": "Lin",
                    },
                ],
            },
        ),
    )


def _table() -> TableArtifact:
    cells = (
        TableCell(
            cell_id="cell-00",
            row_index=0,
            column_index=0,
            text="Environment",
            value="Environment",
            cell_type=TableCellType.HEADER,
            is_header=True,
        ),
        TableCell(
            cell_id="cell-01",
            row_index=0,
            column_index=1,
            text="Timeout",
            value="Timeout",
            cell_type=TableCellType.HEADER,
            is_header=True,
        ),
        TableCell(
            cell_id="cell-10",
            row_index=1,
            column_index=0,
            text="Production",
            value="Production",
        ),
        TableCell(
            cell_id="cell-11",
            row_index=1,
            column_index=1,
            text="30 seconds",
            value="30 seconds",
        ),
    )
    return TableArtifact(
        table_id="table-1",
        table_version_id="table-version-1",
        document_id="document-1",
        document_version_id="version-1",
        source_version="7",
        source_block_id="table-element-1",
        ordinal=0,
        caption="Worker limits",
        row_count=2,
        column_count=2,
        header_row_indices=(0,),
        column_names=("Environment", "Timeout"),
        header_hierarchy=(("Environment",), ("Timeout",)),
        cells=cells,
        logical_grid=(
            (
                TableGridSlot(cell_id="cell-00"),
                TableGridSlot(cell_id="cell-01"),
            ),
            (
                TableGridSlot(cell_id="cell-10"),
                TableGridSlot(cell_id="cell-11"),
            ),
        ),
        content_hash="c" * 64,
    )


@pytest.mark.asyncio
async def test_comment_artifact_is_typed_ordered_and_replay_safe() -> None:
    store = MemoryObjectStore()
    async with store:
        writer = ImmutableArtifactWriter(store)
        reader = ImmutableArtifactReader(store)
        repository = CanonicalCommentArtifactRepository(writer, reader)

        comments, first = await repository.put(
            _document(),
            document_version_id="version-1",
            context=_context(),
        )
        replayed_comments, replayed = await repository.put(
            _document(),
            document_version_id="version-1",
            context=_context(),
        )

        assert first == replayed
        assert comments == replayed_comments
        assert first.key == "comments/document-1/version-1.json"
        assert tuple(item.comment_id for item in comments.comments) == (
            "comment-1",
            "comment-2",
        )
        assert comments.comments[0].body == "Done"
        assert comments.comments[1].body == ("The rollout finished successfully.")
        assert comments.comments[1].author == "Ada"
        assert await repository.get(first, context=_context()) == comments


@pytest.mark.asyncio
async def test_table_artifact_is_real_parquet_with_exact_logical_rows() -> None:
    store = MemoryObjectStore()
    async with store:
        writer = ImmutableArtifactWriter(store)
        reader = ImmutableArtifactReader(store)
        repository = CanonicalTableArtifactRepository(writer, reader)

        references = await repository.put_all(
            (_table(),),
            document_id="document-1",
            document_version_id="version-1",
            context=_context(),
        )
        replayed = await repository.put_all(
            (_table(),),
            document_id="document-1",
            document_version_id="version-1",
            context=_context(),
        )

        assert references == replayed
        reference = references[0]
        assert reference.key == ("tables/document-1/version-1/table-1.parquet")
        assert reference.media_type == "application/vnd.apache.parquet"
        payload = await reader.get(reference, context=_context())
        assert payload[:4] == b"PAR1"
        assert payload[-4:] == b"PAR1"
        assert await repository.get_rows(
            reference,
            context=_context(),
        ) == (
            ("Environment", "Timeout"),
            ("Production", "30 seconds"),
        )

        import pyarrow.parquet as parquet

        metadata = parquet.read_metadata(__import__("pyarrow").BufferReader(payload)).metadata
        assert metadata is not None
        descriptor = json.loads(metadata[b"harborrag.table"])
        assert descriptor["table_id"] == "table-1"
        assert descriptor["row_count"] == 2


@pytest.mark.asyncio
async def test_table_artifact_rejects_document_version_drift() -> None:
    store = MemoryObjectStore()
    async with store:
        repository = CanonicalTableArtifactRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )

        with pytest.raises(
            ValueError,
            match="another document version",
        ):
            await repository.put_all(
                (_table(),),
                document_id="document-1",
                document_version_id="version-2",
                context=_context(),
            )
