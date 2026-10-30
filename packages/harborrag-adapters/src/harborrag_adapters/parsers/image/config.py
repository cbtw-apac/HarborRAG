"""Image parser configuration."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.parsers.common.limits import IMAGE_MAX_PIXELS


@dataclass(frozen=True, slots=True)
class ImageParserConfig:
    """Image OCR provider and resource limits."""

    engine: str = "liteparse"
    language: str | None = None
    max_pixels: int | None = IMAGE_MAX_PIXELS
