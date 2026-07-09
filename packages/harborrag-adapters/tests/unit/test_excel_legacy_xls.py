"""Unit tests for Excel parser legacy .xls behavior."""
from __future__ import annotations

import io

import pytest

from harborrag_core.domain.parser import ParseInput


pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _xls_bytes() -> bytes:
    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("Data")
    for r, row in enumerate([["name", "score"], ["Ada", 42], ["Bob", 3.5]]):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_excel_parser_reads_legacy_xls() -> None:
    from harborrag_adapters.parsers.excel import ExcelParser

    doc = ExcelParser().parse(ParseInput(content=_xls_bytes(), filename="legacy.xls"))
    assert "Data" in doc.content
    assert "Ada" in doc.content and "42" in doc.content
    assert doc.metadata["sheets"] == ["Data"]


def test_excel_parser_missing_dependency_raises_parse_error(monkeypatch) -> None:
    import builtins

    from harborrag_adapters.parsers.exceptions import ParseError
    from harborrag_adapters.parsers.excel import ExcelParser

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ParseError, match="openpyxl"):
        ExcelParser().parse(ParseInput(content=b"PK\x03\x04", filename="x.xlsx"))
