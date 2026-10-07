"""Image parser configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageParserConfig:
    """Image OCR provider and resource limits."""

    engine: str = "liteparse"
    language: str | None = None
    max_pixels: int | None = 100_000_000
