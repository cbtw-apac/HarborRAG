"""Presentation parser configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PresentationParserConfig:
    """Enabled presentation engine."""

    engine: str = "python_pptx"
