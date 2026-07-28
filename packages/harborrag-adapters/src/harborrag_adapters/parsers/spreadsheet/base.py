"""Spreadsheet engine contract."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from harborrag_adapters.parsers.common.base import HarborParserEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput


class HarborSpreadsheetEngine(
    HarborParserEngine[ParseInput, ParsedDocument],
    ABC,
):
    """Provider contract for sheets, formulas, merged cells, and tabular ranges."""

    supports_formulas: ClassVar[bool] = False
    supports_merged_cells: ClassVar[bool] = False
