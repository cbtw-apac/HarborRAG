"""CSV engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CsvEngineConfig:
    """Delimiter and decoding defaults for delimited text."""

    delimiter: str | None = None
