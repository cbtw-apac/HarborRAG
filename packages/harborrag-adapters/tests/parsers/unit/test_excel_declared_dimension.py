"""A worksheet whose <dimension> spans the whole sheet must still parse."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook

from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.rendering import (
    open_data_workbook,
)

_WHOLE_SHEET = 'ref="A1:Z1048576"'


def _workbook_bytes(*, whole_sheet_dimension: bool) -> bytes:
    """Build a tiny workbook, optionally lying about its dimension.

    Tool-generated workbooks routinely emit a <dimension> covering the entire sheet, which
    is what a Confluence attachment did in production. Patching the record is how the test
    reproduces that without shipping the customer's file.
    """

    book = Workbook()
    sheet = book.active
    sheet.title = "data"
    sheet.append(["Story ID", "Objective"])
    sheet.append(["S-1", "Onboard"])
    buffer = BytesIO()
    book.save(buffer)
    original = buffer.getvalue()
    if not whole_sheet_dimension:
        return original

    source = zipfile.ZipFile(BytesIO(original))
    patched = BytesIO()
    with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as out:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/"):
                payload = re.sub(rb'ref="[A-Z0-9:]+"', _WHOLE_SHEET.encode(), payload, count=1)
            out.writestr(item, payload)
    return patched.getvalue()


def test_a_whole_sheet_dimension_resolves_to_the_real_shape() -> None:
    """Read-only mode pads iter_rows out to the declaration, so trusting it both rejects
    the file as hostile and walks a million empty rows. Measured in production: declared
    1048576 x 26, really 790 x 26, parser_rejected_document on every ingestion."""

    data = _workbook_bytes(whole_sheet_dimension=True)

    lying = load_workbook(BytesIO(data), read_only=True, data_only=True)
    assert lying.worksheets[0].max_row == 1048576
    lying.close()

    workbook = open_data_workbook(load_workbook, data)
    try:
        sheet = workbook.worksheets[0]
        assert sheet.max_row < 1048576
        rows = [row for row in sheet.iter_rows(values_only=True) if any(row)]
        assert rows[0] == ("Story ID", "Objective")
        assert len(rows) == 2
    finally:
        workbook.close()


def test_an_honest_dimension_keeps_the_streaming_reader() -> None:
    """The fallback is only for the uninformative case; everything else stays read-only."""

    workbook = open_data_workbook(load_workbook, _workbook_bytes(whole_sheet_dimension=False))
    try:
        assert workbook.read_only is True
        assert workbook.worksheets[0].max_row == 2
    finally:
        workbook.close()


@pytest.mark.parametrize("whole_sheet", [True, False])
def test_the_same_cells_are_read_either_way(whole_sheet: bool) -> None:
    workbook = open_data_workbook(load_workbook, _workbook_bytes(whole_sheet_dimension=whole_sheet))
    try:
        rows = [row for row in workbook.worksheets[0].iter_rows(values_only=True) if any(row)]
    finally:
        workbook.close()

    assert rows == [("Story ID", "Objective"), ("S-1", "Onboard")]
