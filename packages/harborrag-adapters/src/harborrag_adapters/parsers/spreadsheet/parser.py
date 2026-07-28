"""Complete spreadsheet-family parsing workflow."""

from __future__ import annotations

from harborrag_adapters.parsers.common.family import (
    HarborSingleEngineFamilyParser,
    SingleEngineRouter,
)
from harborrag_adapters.parsers.spreadsheet.base import HarborSpreadsheetEngine
from harborrag_adapters.parsers.spreadsheet.normalization import SpreadsheetNormalizer


class HarborSpreadsheetParser(HarborSingleEngineFamilyParser):
    """Select a spreadsheet engine and return normalized tabular content."""

    parser_name = "spreadsheet"

    def __init__(
        self,
        engines: tuple[HarborSpreadsheetEngine, ...],
    ) -> None:
        super().__init__(
            SingleEngineRouter("spreadsheet", engines),
            SpreadsheetNormalizer(),
        )
