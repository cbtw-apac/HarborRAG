"""Profiles and policy configuration for PDF engine routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PDFParserProfile(StrEnum):
    """Named PDF engine orderings for cost and quality tradeoffs."""

    FAST = "fast"
    BALANCED = "balanced"
    OCR = "ocr"
    OCR_FIRST = "ocr_first"
    QUALITY = "quality"
    SCIENTIFIC = "scientific"

    @classmethod
    def normalize(cls, value: PDFParserProfile | str) -> PDFParserProfile:
        if isinstance(value, cls):
            return value
        normalized = str(value).lower().strip()
        try:
            return cls(normalized)
        except ValueError as error:
            supported = ", ".join(profile.value for profile in cls)
            raise ValueError(f"Unknown PDF parser profile {value!r}: {supported}") from error


@dataclass(frozen=True, slots=True)
class PDFProfileConfig:
    """Engine order and acceptance policy for one PDF parsing profile."""

    engine_order: tuple[str, ...]
    minimum_quality_score: float
    preserve_equations: bool = False
    preserve_tables: bool = False
    preserve_layout: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_quality_score <= 1:
            raise ValueError("PDF minimum_quality_score must be between 0 and 1")


def default_pdf_profiles() -> dict[str, PDFProfileConfig]:
    return {
        "fast": PDFProfileConfig(("pymupdf", "liteparse"), 0.65),
        "balanced": PDFProfileConfig(
            ("pymupdf", "docling", "liteparse", "mineru", "paddleocr"),
            0.80,
        ),
        "ocr": PDFProfileConfig(
            ("docling", "mineru", "paddleocr", "pymupdf"),
            0.75,
            preserve_tables=True,
            preserve_layout=True,
        ),
        "ocr_first": PDFProfileConfig(
            ("paddleocr", "mineru", "docling", "pymupdf"),
            0.75,
            preserve_tables=True,
            preserve_layout=True,
        ),
        "quality": PDFProfileConfig(
            ("docling", "mineru", "paddleocr", "pymupdf", "liteparse"),
            0.85,
            preserve_equations=True,
            preserve_tables=True,
            preserve_layout=True,
        ),
        "scientific": PDFProfileConfig(
            ("mineru", "docling", "pymupdf"),
            0.85,
            preserve_equations=True,
            preserve_tables=True,
            preserve_layout=True,
        ),
    }


@dataclass(frozen=True, slots=True)
class PDFRouterConfig:
    """Default profile and named engine-order policies."""

    default_profile: str = "balanced"
    profiles: dict[str, PDFProfileConfig] = field(default_factory=default_pdf_profiles)

    def __post_init__(self) -> None:
        if self.default_profile not in self.profiles:
            raise ValueError(f"Unknown default PDF profile: {self.default_profile!r}")


@dataclass(frozen=True, slots=True)
class PDFParserConfig:
    """PDF parser quality threshold and routing profiles."""

    min_content_chars: int = 20
    router: PDFRouterConfig = field(default_factory=PDFRouterConfig)

    def __post_init__(self) -> None:
        if self.min_content_chars < 0:
            raise ValueError("PDF min_content_chars cannot be negative")
