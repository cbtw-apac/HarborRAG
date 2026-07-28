"""Spreadsheet output normalization policy."""

from harborrag_adapters.parsers.common.family import FamilyResultNormalizer


class SpreadsheetNormalizer(FamilyResultNormalizer):
    """Convert spreadsheet engine output into the stable parser result."""

    def __init__(self) -> None:
        super().__init__("spreadsheet")
