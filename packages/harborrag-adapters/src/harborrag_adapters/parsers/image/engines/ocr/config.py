"""Local OCR engine configuration."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.parsers.common.limits import IMAGE_MAX_PIXELS


@dataclass(frozen=True, slots=True)
class OcrEngineConfig:
    """Selectable local OCR implementation and resource controls."""

    provider: str = "liteparse"
    language: str | None = None
    timeout: int | float | None = 60
    max_pixels: int | None = IMAGE_MAX_PIXELS
