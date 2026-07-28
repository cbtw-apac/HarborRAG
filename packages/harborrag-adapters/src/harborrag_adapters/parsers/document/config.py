"""Configuration for the document parser family."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentParserConfig:
    """Enabled document engines in deterministic route order."""

    engine_order: tuple[str, ...] = ("docx", "odt", "epub")
