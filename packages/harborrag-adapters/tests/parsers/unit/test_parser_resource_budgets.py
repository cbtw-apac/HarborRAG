"""Resource-budget regressions for parsers handling untrusted structured data."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.common.validation import ParseResourceBudget
from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.spreadsheet.engines.csv import engine as csv_engine
from harborrag_adapters.parsers.spreadsheet.engines.openpyxl.engine import (
    ExcelSpreadsheetEngine,
)
from harborrag_adapters.parsers.structured.engines.json.engine import JsonStructuredEngine
from harborrag_core.domain.parser import ParseInput

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_resource_budget_rejects_rows_cells_and_output() -> None:
    row_budget = ParseResourceBudget(max_rows=1)
    row_budget.consume_row(1)
    with pytest.raises(ParseError, match="row count"):
        row_budget.consume_row(1)

    cell_budget = ParseResourceBudget(max_cells=1)
    with pytest.raises(ParseError, match="cell count"):
        cell_budget.consume_row(2)

    output_budget = ParseResourceBudget(max_output_characters=3)
    with pytest.raises(ParseError, match="character limit"):
        output_budget.consume_output(4)


def test_csv_rejects_excessive_physical_rows_before_splitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csv_engine, "MAX_TABLE_ROWS", 2)

    with pytest.raises(ParseError, match="physical row count"):
        csv_engine.CsvSpreadsheetEngine().parse(
            ParseInput(content="a\n1\n2\n", filename="too-many.csv")
        )


def test_json_flatten_rejects_excessive_rendered_output() -> None:
    budget = ParseResourceBudget(max_output_characters=8)

    with pytest.raises(ParseError, match="character limit"):
        JsonStructuredEngine._flatten({"long": "value"}, budget=budget)


def test_excel_rejects_hostile_declared_dimensions_before_iteration() -> None:
    budget = ParseResourceBudget(max_rows=10, max_cells=20)

    with pytest.raises(ParseError, match="cell count"):
        ExcelSpreadsheetEngine._guard_declared_table_size(
            rows=5,
            columns=5,
            budget=budget,
        )
