from __future__ import annotations

import io
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from harborrag_adapters.parsers.compat import PptxParser
from harborrag_core.domain.parser import ParsedDocument, ParseInput

if TYPE_CHECKING:
    from pptx.table import Table

pytestmark = pytest.mark.unit

_ROWS = [
    ["Check Area", "Expected Result"],
    ["Upload", "Completed successfully"],
    ["Preview", "Readable in English"],
]


def test_pptx_table_renders_as_markdown_table_after_the_slide_title() -> None:
    parsed = _parse(lambda table: None)

    assert parsed.elements[0].content == (
        "Validation\n\n"
        "| Check Area | Expected Result |\n"
        "| --- | --- |\n"
        "| Upload | Completed successfully |\n"
        "| Preview | Readable in English |"
    )


def test_pptx_merged_cell_is_shown_once_with_its_span() -> None:
    parsed = _parse(lambda table: table.cell(1, 0).merge(table.cell(1, 1)))

    assert "| Upload<br>Completed successfully (spans 2 columns) |  |" in (
        parsed.elements[0].content.splitlines()
    )


def test_pptx_table_without_header_row_keeps_first_row_as_data() -> None:
    def no_header(table: Table) -> None:
        table.first_row = False

    parsed = _parse(no_header)

    assert parsed.elements[0].content.splitlines()[2:4] == [
        "| Column 1 | Column 2 |",
        "| --- | --- |",
    ]


def _parse(change_table: Callable[[Table], object]) -> ParsedDocument:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Validation"
    shape = slide.shapes.add_table(len(_ROWS), 2, Inches(1), Inches(2), Inches(6), Inches(2))
    for row_index, row in enumerate(_ROWS):
        for column_index, text in enumerate(row):
            shape.table.cell(row_index, column_index).text = text
    change_table(shape.table)
    buffer = io.BytesIO()
    deck.save(buffer)
    return PptxParser().parse(ParseInput(content=buffer.getvalue(), filename="deck.pptx"))
