"""Configuration for spreadsheet parsing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpreadsheetParserConfig:
    """Enabled spreadsheet engines in deterministic route order."""

    engine_order: tuple[str, ...] = ("excel", "csv")
