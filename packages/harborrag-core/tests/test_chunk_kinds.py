from __future__ import annotations

import pytest
from chunking_test_fixtures import make_chunk

from harborrag_core.chunking import ChunkKind, TableChunkLocator

pytestmark = pytest.mark.unit

_TABLE_TEXT = "Expected results:\n\n| Step | Result |\n| --- | --- |\n| 1 | Saved |\n\nDone."


@pytest.mark.parametrize(
    ("chunk_kind", "content", "expected"),
    [
        (ChunkKind.TEXT, "Plain prose with a | pipe | inside.", (ChunkKind.TEXT,)),
        (ChunkKind.TEXT, _TABLE_TEXT, (ChunkKind.TEXT, ChunkKind.TABLE)),
        (ChunkKind.COMMENT, _TABLE_TEXT, (ChunkKind.COMMENT, ChunkKind.TABLE)),
        (ChunkKind.TEXT, "| 3 | Retry |\n| 4 | Done |", (ChunkKind.TEXT, ChunkKind.TABLE)),
        (ChunkKind.TEXT, "| only one row |\nthen prose", (ChunkKind.TEXT,)),
    ],
    ids=["prose", "text-with-table", "comment-with-table", "table-rows-only", "single-row"],
)
def test_chunk_kinds_lists_table_when_content_holds_a_markdown_table(
    chunk_kind: ChunkKind,
    content: str,
    expected: tuple[ChunkKind, ...],
) -> None:
    chunk = make_chunk(chunk_kind=chunk_kind, content=content)

    assert chunk.chunk_kinds == expected


def test_chunk_kinds_of_a_table_chunk_is_table_only() -> None:
    locator = TableChunkLocator(
        table_id="table:stable",
        table_version_id="table-version:7",
        row_start=0,
        row_end=1,
        column_count=2,
    )

    chunk = make_chunk(chunk_kind=ChunkKind.TABLE, table_locator=locator, content=_TABLE_TEXT)

    assert chunk.chunk_kinds == (ChunkKind.TABLE,)


def test_chunk_kinds_is_not_serialized_with_the_canonical_record() -> None:
    serialized = make_chunk(content=_TABLE_TEXT).model_dump(mode="json")

    assert serialized["chunk_kind"] == "text"
    assert "chunk_kinds" not in serialized
