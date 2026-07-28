"""Structured-data parser configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StructuredParserConfig:
    """Enabled structured-data engines."""

    engine_order: tuple[str, ...] = ("json",)
