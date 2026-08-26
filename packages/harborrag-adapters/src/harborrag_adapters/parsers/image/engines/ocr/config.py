"""Local OCR engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OcrEngineConfig:
    """Selectable local OCR implementation and resource controls."""

    provider: str = "pytesseract"
    language: str | None = None
    timeout: int | float | None = 60
    max_pixels: int | None = 100_000_000
