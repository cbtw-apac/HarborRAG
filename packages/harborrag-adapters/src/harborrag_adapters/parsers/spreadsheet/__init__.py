"""Spreadsheet-family parser and engine contract."""

from harborrag_adapters.parsers.spreadsheet.base import HarborSpreadsheetEngine
from harborrag_adapters.parsers.spreadsheet.parser import HarborSpreadsheetParser

__all__ = ["HarborSpreadsheetEngine", "HarborSpreadsheetParser"]
