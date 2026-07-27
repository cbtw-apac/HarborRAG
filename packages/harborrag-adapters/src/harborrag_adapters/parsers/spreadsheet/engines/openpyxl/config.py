"""Excel engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OpenPyxlEngineConfig:
    """Workbook-loading defaults for modern Excel documents."""

    data_only: bool = False
